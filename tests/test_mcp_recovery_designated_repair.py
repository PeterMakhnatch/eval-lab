"""Focused tests for MCP Recovery v1 exact designated repair moves and causal mutation evidence."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from evallab.benchmark_program_contracts import FaultClass

ROOT = Path(__file__).resolve().parents[1] / "library" / "benchmarks" / "mcp-recovery-v1"


def _benchmark_stem_names() -> set[str]:
    return {p.stem for p in ROOT.glob("*.py")}


def _restore_benchmark_modules(saved: dict[str, object], stems: set[str]) -> None:
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


def load(name: str):
    module_name = f"mcp_recovery_v1_repair_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    stems = _benchmark_stem_names()
    saved = {stem: sys.modules.get(stem) for stem in stems}
    for stem in stems:
        sys.modules.pop(stem, None)
    orig_path = list(sys.path)
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    finally:
        sys.path[:] = orig_path
        _restore_benchmark_modules(saved, stems)


def test_contract_binds_designated_repair_moves():
    contract_mod = load("contract")
    contract = contract_mod.get_benchmark_contract()
    designated_moves = contract["cell_factors"]["designated_repair_moves"]

    assert len(designated_moves) == 5
    assert designated_moves[FaultClass.PERSISTENT_SIGNATURE_ERROR.value] == "refresh_auth"
    assert designated_moves[FaultClass.PERSISTENT_SCHEMA_MISMATCH.value] == "fallback_query"
    assert designated_moves[FaultClass.TRANSIENT_NETWORK_TIMEOUT.value] == "refresh_auth"
    assert designated_moves[FaultClass.TRANSIENT_HTTP_5XX.value] == "fallback_query"
    assert designated_moves[FaultClass.SILENT_WRONG_PAYLOAD.value] == "fallback_query"


@pytest.mark.parametrize("fault", list(FaultClass))
@pytest.mark.parametrize("persistence", [1, 2])
def test_all_five_fault_classes_oracle_and_clean_twins(tmp_path, fault, persistence):
    materializer = load("materializer")
    templates = load("templates")
    verifier = load("verifier")

    key = os.urandom(32)
    seed = 42

    # Materialize fault arm
    fault_task = materializer.materialize_task(
        tmp_path / f"fault_{fault.value}_p{persistence}",
        seed=seed,
        fault_mode=fault,
        persistence=persistence,
        is_clean_twin=False,
        evidence_key=key,
    )

    # Materialize matched clean twin arm
    clean_task = materializer.materialize_task(
        tmp_path / f"clean_{fault.value}_p{persistence}",
        seed=seed,
        fault_mode=fault,
        persistence=persistence,
        is_clean_twin=True,
        evidence_key=key,
    )

    fault_record = json.loads((fault_task / "tests/fixtures/fault_record.json").read_text())
    clean_record = json.loads((clean_task / "tests/fixtures/fault_record.json").read_text())

    # Reciprocal twin identity binding
    assert fault_record["task_id"] == clean_record["twin_task_id"]
    assert clean_record["task_id"] == fault_record["twin_task_id"]
    assert fault_record["injection_payload"]["designated_repair_move"] == materializer.get_designated_repair(fault)

    # Oracle on fault arm achieves reward 1.0 with exact causal mutation and mutation digest verified
    templates.run_oracle_repair(fault_task, fault_task / "agent_ws")
    fault_res = verifier.verify_harbor_task(fault_task)
    assert fault_res["reward"] == 1.0, fault_res
    assert fault_res["success"] is True
    assert fault_res["causal_mutation"] is True
    assert fault_res["designated_repair_matched"] is True
    assert fault_res["mutation_digest_verified"] is True
    assert fault_res["fault_count"] == persistence

    # Oracle on clean twin arm achieves reward 1.0 with zero faults
    templates.run_oracle_repair(clean_task, clean_task / "agent_ws")
    clean_res = verifier.verify_harbor_task(clean_task)
    assert clean_res["reward"] == 1.0, clean_res
    assert clean_res["success"] is True
    assert clean_res["zero_faults"] is True
    assert clean_res["mutation_digest_verified"] is True
    assert clean_res["fault_count"] == 0


@pytest.mark.parametrize("fault", list(FaultClass))
@pytest.mark.parametrize("persistence", [1, 2])
def test_designated_repair_negative_controls_fail(tmp_path, fault, persistence):
    materializer = load("materializer")
    templates = load("templates")
    verifier = load("verifier")

    key = os.urandom(32)
    seed = 42

    task = materializer.materialize_task(
        tmp_path / f"fault_neg_{fault.value}_p{persistence}",
        seed=seed,
        fault_mode=fault,
        persistence=persistence,
        is_clean_twin=False,
        evidence_key=key,
    )

    # 1. Blind retry mutant -> reward 0.0 (no causal mutation)
    templates.run_blind_retry_control(task, task / "agent_ws")
    res_blind = verifier.verify_harbor_task(task)
    assert res_blind["reward"] == 0.0, res_blind
    assert res_blind["causal_mutation"] is False

    # 2. Wrong repair mutant (non-designated tool) -> reward 0.0
    templates.run_wrong_repair_mutant(task, task / "agent_ws")
    res_wrong = verifier.verify_harbor_task(task)
    assert res_wrong["reward"] == 0.0, res_wrong
    assert res_wrong["designated_repair_matched"] is False

    # 3. Unconditional fallback mutant -> reward 0.0
    templates.run_unconditional_fallback_mutant(task, task / "agent_ws")
    res_uncond = verifier.verify_harbor_task(task)
    assert res_uncond["reward"] == 0.0, res_uncond
    assert res_uncond["causal_mutation"] is False


def test_clean_twin_negative_controls(tmp_path):
    materializer = load("materializer")
    templates = load("templates")
    verifier = load("verifier")

    key = os.urandom(32)
    clean_task = materializer.materialize_task(
        tmp_path / "clean_ctrl",
        seed=42,
        fault_mode=FaultClass.PERSISTENT_SIGNATURE_ERROR,
        persistence=1,
        is_clean_twin=True,
        evidence_key=key,
    )

    # Unconfirmed write mutant -> reward 0.0
    templates.run_unconfirmed_write_mutant(clean_task, clean_task / "agent_ws")
    res_unconfirmed = verifier.verify_harbor_task(clean_task)
    assert res_unconfirmed["reward"] == 0.0
    assert res_unconfirmed["read_ok"] is False

    # Wrong repair mutant on clean arm -> reward 0.0
    templates.run_wrong_repair_mutant(clean_task, clean_task / "agent_ws")
    res_wrong = verifier.verify_harbor_task(clean_task)
    assert res_wrong["reward"] == 0.0


def test_tampered_mutation_digest_fails_verification(tmp_path):
    materializer = load("materializer")
    templates = load("templates")
    verifier = load("verifier")
    envelope_mod = load("envelope")

    key = os.urandom(32)
    task = materializer.materialize_task(
        tmp_path / "tamper_task",
        seed=42,
        fault_mode=FaultClass.PERSISTENT_SIGNATURE_ERROR,
        persistence=1,
        is_clean_twin=False,
        evidence_key=key,
    )

    # Run oracle to produce valid state
    templates.run_oracle_repair(task, task / "agent_ws")

    # Tamper with the mutation digest in sealed evidence
    env_file = task / "output/sealed-evidence.json"
    raw_env = json.loads(env_file.read_text())
    payload = envelope_mod.decrypt_envelope(
        key,
        raw_env,
        task_id=task.name,
        fault_id="opaque",
        persistence=1,
    )
    payload["mutation_digest"] = "0000000000000000000000000000000000000000000000000000000000000000"

    tampered_env = envelope_mod.encrypt_envelope(
        key,
        payload,
        task_id=task.name,
        fault_id="opaque",
        persistence=1,
        sequence=payload["sequence"],
    )
    env_file.write_text(json.dumps(tampered_env) + "\n")

    res = verifier.verify_harbor_task(task)
    assert res["reward"] == 0.0
    assert res["mutation_digest_verified"] is False
