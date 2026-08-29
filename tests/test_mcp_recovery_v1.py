from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evallab.benchmark_program_contracts import FaultClass
from evallab.task_workbench import CandidateSource, inspect_candidate

ROOT = Path(__file__).resolve().parents[1] / "library" / "benchmarks" / "mcp-recovery-v1"


def _benchmark_stem_names() -> set[str]:
    """Top-level module names this benchmark can define (its ``.py`` files)."""
    return {p.stem for p in ROOT.glob("*.py")}


def _restore_benchmark_modules(saved: dict[str, object], stems: set[str]) -> None:
    """Drop this benchmark's own bare imports, then restore the prior entries.

    Sibling benchmark families share generic module names (``state``, ``source``,
    ``envelope``, ``templates``, ...). To load this benchmark we temporarily evict
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


def load(name: str):
    module_name = f"mcp_recovery_v1_{name}"
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
        spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
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


def test_envelope_cryptography_and_tamper_proofing():
    envelope_mod = load("envelope")
    key = os.urandom(32)
    payload = {
        "sequence": 5,
        "records": {"operational-key": "verified-value"},
        "events": [{"event_index": 0, "tool": "write_record", "outcome": "ok"}],
        "initial_digest": "init",
        "final_digest": "final",
    }
    sealed = envelope_mod.encrypt_envelope(
        key,
        payload,
        task_id="task_main",
        fault_id="fault_root",
        persistence=2,
        sequence=5,
    )
    assert set(sealed) == {"schema_version", "sequence", "nonce", "ciphertext"}
    assert sealed["sequence"] == 5

    # Valid decryption
    decrypted = envelope_mod.decrypt_envelope(
        key,
        sealed,
        task_id="task_main",
        fault_id="fault_root",
        persistence=2,
    )
    assert decrypted == payload

    # Tampered sequence in outer metadata fails AAD authentication
    with pytest.raises(ValueError):
        envelope_mod.decrypt_envelope(
            key,
            dict(sealed, sequence=6),
            task_id="task_main",
            fault_id="fault_root",
            persistence=2,
        )

    # Wrong task_id or fault_id fails AAD authentication
    with pytest.raises(ValueError):
        envelope_mod.decrypt_envelope(
            key,
            sealed,
            task_id="wrong_task",
            fault_id="fault_root",
            persistence=2,
        )

    # Wrong key fails decryption
    with pytest.raises(ValueError):
        envelope_mod.decrypt_envelope(
            os.urandom(32),
            sealed,
            task_id="task_main",
            fault_id="fault_root",
            persistence=2,
        )


def test_contract_schema_and_cells():
    contract = load("contract").get_benchmark_contract()
    assert contract["family"] == "mcp-recovery-v1"
    assert contract["synthetic_family"] == "family_c_fault_recovery"
    assert contract["cell_count"] == 10
    assert contract["total_task_count"] == 20
    assert contract["cell_factors"]["matched_arms"] == ["fault", "clean_twin"]
    assert contract["evidence_contract"]["sealed_envelope_path"] == "/app/output/sealed-evidence.json"


def test_generated_verifier_is_syntactically_valid(tmp_path):
    import py_compile
    materializer = load("materializer")
    task = materializer.materialize_task(tmp_path / "task", seed=42, evidence_key=os.urandom(32))
    verify_script = task / "tests/verify.py"
    assert verify_script.is_file()
    py_compile.compile(str(verify_script), doraise=True)

def test_materializer_hard_fails_without_evidence_key(monkeypatch, tmp_path):
    materializer = load("materializer")
    monkeypatch.delenv("MCP_RECOVERY_EVIDENCE_KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="Evidence key is required"):
        materializer.materialize_task(tmp_path / "task_no_key", seed=42, evidence_key=None)


def test_evidence_key_file_rejects_insecure_permissions(tmp_path):
    materializer = load("materializer")
    insecure_key = tmp_path / "insecure_key.bin"
    insecure_key.write_bytes(os.urandom(32))
    os.chmod(insecure_key, 0o666)

    with pytest.raises(ValueError, match="insecure permissions"):
        materializer.materialize_task(tmp_path / "task", seed=42, evidence_key=insecure_key)

def test_public_factor_enumeration_cannot_predict_slug_or_decrypt_envelope(tmp_path):
    materializer = load("materializer")
    envelope_mod = load("envelope")
    key = os.urandom(32)
    task = materializer.materialize_task(tmp_path / "task", seed=42, fault_mode=FaultClass.PERSISTENT_SIGNATURE_ERROR, persistence=1, evidence_key=key)

    # 1. Slug cannot be predicted by hashing public factors without key
    public_hash = materializer.compute_sha256("mcp-recovery-domain:42:persistent_signature_error:1:fault")[:16]
    assert public_hash not in task.name

    # 2. Decrypting envelope with unkeyed public metadata fails
    env = envelope_mod.encrypt_envelope(key, {"test": True, "sequence": 1}, task_id=task.name, fault_id="opaque", persistence=1, sequence=1)
    unkeyed_guess = hashlib.sha256(b"public_metadata_guess").digest()
    with pytest.raises(ValueError):
        envelope_mod.decrypt_envelope(unkeyed_guess, env, task_id=task.name, fault_id="opaque", persistence=1)


def test_one_delta_clean_twin_matching(tmp_path):
    materializer = load("materializer")
    for fault in FaultClass:
        for persistence in (1, 2):
            key = os.urandom(32)
            fault_task = materializer.materialize_task(tmp_path / f"fault_{fault.value}_{persistence}", seed=42, fault_mode=fault, persistence=persistence, is_clean_twin=False, evidence_key=key)
            clean_task = materializer.materialize_task(tmp_path / f"clean_{fault.value}_{persistence}", seed=42, fault_mode=fault, persistence=persistence, is_clean_twin=True, evidence_key=key)

            fault_record = json.loads((fault_task / "tests/fixtures/fault_record.json").read_text())
            clean_record = json.loads((clean_task / "tests/fixtures/fault_record.json").read_text())

            assert fault_record["task_id"] == clean_record["twin_task_id"]
            assert clean_record["task_id"] == fault_record["twin_task_id"]
            assert fault_record["injection_payload"]["is_clean_twin"] is False
            assert clean_record["injection_payload"]["is_clean_twin"] is True
            assert clean_record["injection_payload"]["persistence"] == 0

            # Instruction and Dockerfiles are byte-identical
            assert (fault_task / "instruction.md").read_bytes() == (clean_task / "instruction.md").read_bytes()
            assert (fault_task / "environment/Dockerfile").read_bytes() == (clean_task / "environment/Dockerfile").read_bytes()


def test_security_boundary_and_zero_truth_leakage(tmp_path):
    materializer = load("materializer")
    key = os.urandom(32)
    task = materializer.materialize_task(tmp_path / "task", seed=42, evidence_key=key)

    visible_files = [task / "instruction.md", task / "task.toml", task / "environment" / "Dockerfile"]
    if (task / "environment" / "docker-compose.yaml").is_file():
        visible_files.append(task / "environment" / "docker-compose.yaml")
    agent_visible = "\n".join(path.read_text(encoding="utf-8") for path in visible_files)

    forbidden = (
        "persistent_signature_error",
        "persistent_schema_mismatch",
        "silent_wrong_payload",
        "transient_http_5xx",
        "transient_network_timeout",
        "secret_key",
        "fault_id",
        "fault_class",
        "admin_write",
        "verified-payload",
        "operational-record",
        "clean_twin",
        "clean-twin",
        "base_task_pair_id",
    )
    for token in forbidden:
        assert token not in agent_visible, f"Leaked token '{token}' in agent-visible bytes"

    task_toml = (task / "task.toml").read_text()
    assert "/app/output/sealed-evidence.json" in task_toml
    assert "[[verifier.collect]]" not in task_toml
    assert "local-lab/mcp-rec-" in task_toml
    assert not any(f.value in task.name for f in FaultClass)


def test_target_specific_causal_recovery_flows():
    verifier = load("verifier")
    envelope_mod = load("envelope")
    key = os.urandom(32)

    # 1. Silent read-targeted fault: initial write -> N corrupt reads -> mutation -> clean read (no post-mutation write needed)
    silent_events = [
        {"event_index": 0, "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "k", "written_value": "v"},
        {"event_index": 1, "tool": "read_record", "outcome": "silent_corruption", "fault_injected": True, "fault_id": "f_silent", "read_key": "k"},
        {"event_index": 2, "tool": "fallback_query", "outcome": "ok", "fault_injected": False},
        {"event_index": 3, "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "k", "read_value": "v"},
    ]
    env_silent = envelope_mod.encrypt_envelope(key, {"sequence": 4, "initial_digest": "i", "final_digest": "f", "records": {"k": "v"}, "events": silent_events}, task_id="t_silent", fault_id="f_silent", persistence=1, sequence=4)
    scored_silent = verifier.score_recovery_envelope(key, env_silent, task_id="t_silent", fault_id="f_silent", expected_persistence=1, is_clean_twin=False, target_tool="read_record")
    assert scored_silent["reward"] == 1.0, scored_silent
    assert scored_silent["causal_mutation"] is True

    # 2. Write-targeted fault: N fault writes -> mutation -> post-mutation write -> matching read
    write_events = [
        {"event_index": 0, "tool": "write_record", "outcome": "error", "fault_injected": True, "fault_id": "f_write", "written_key": "k"},
        {"event_index": 1, "tool": "refresh_auth", "outcome": "ok", "fault_injected": False},
        {"event_index": 2, "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "k", "written_value": "v"},
        {"event_index": 3, "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "k", "read_value": "v"},
    ]
    env_write = envelope_mod.encrypt_envelope(key, {"sequence": 4, "initial_digest": "i", "final_digest": "f", "records": {"k": "v"}, "events": write_events}, task_id="t_write", fault_id="f_write", persistence=1, sequence=4)
    scored_write = verifier.score_recovery_envelope(key, env_write, task_id="t_write", fault_id="f_write", expected_persistence=1, is_clean_twin=False)
    assert scored_write["reward"] == 1.0, scored_write

    # 3. Clean read before mutation permanently disqualifies
    disqualified_events = [
        {"event_index": 0, "tool": "write_record", "outcome": "error", "fault_injected": True, "fault_id": "f_disq", "written_key": "k"},
        {"event_index": 1, "tool": "write_record", "outcome": "ok", "fault_injected": False, "written_key": "k", "written_value": "v"},
        {"event_index": 2, "tool": "read_record", "outcome": "ok", "fault_injected": False, "read_key": "k", "read_value": "v"},
        {"event_index": 3, "tool": "refresh_auth", "outcome": "ok", "fault_injected": False},
    ]
    env_disq = envelope_mod.encrypt_envelope(key, {"sequence": 4, "initial_digest": "i", "final_digest": "f", "records": {"k": "v"}, "events": disqualified_events}, task_id="t_disq", fault_id="f_disq", persistence=1, sequence=4)
    scored_disq = verifier.score_recovery_envelope(key, env_disq, task_id="t_disq", fault_id="f_disq", expected_persistence=1, is_clean_twin=False)
    assert scored_disq["reward"] == 0.0
    assert scored_disq["auto_clear"] is True


def test_sidecar_hard_fails_without_secret_key(tmp_path):
    materializer = load("materializer")
    key = os.urandom(32)
    task_dir = tmp_path / "task_no_key"
    materializer.materialize_task(task_dir, seed=42, evidence_key=key)

    sidecar_key = task_dir / "environment/mcp-server/secret_key.txt"
    if sidecar_key.exists():
        sidecar_key.unlink()

    server_script = task_dir / "environment/mcp-server/server.py"
    res = subprocess.run(
        [sys.executable, str(server_script)],
        cwd=str(task_dir / "environment/mcp-server"),
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "secret_key.txt not provisioned" in res.stderr or "secret_key.txt not provisioned" in res.stdout


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"FastMCP server did not bind 127.0.0.1:{port}")


def _start_fastmcp(tmp_path: Path, fault: FaultClass, persistence: int, is_clean_twin: bool = False) -> tuple[subprocess.Popen, Path, int, bytes, dict]:
    # Core proof-bound generator writes its canonical event middleware under /app/output.
    # Local host execution without that container filesystem is not representative; CI Harbor Docker exercises it.
    if not Path("/app").is_dir():
        pytest.skip("live proof-bound sidecar requires the Docker /app runtime filesystem")
    materializer = load("materializer")
    task_dir = tmp_path / "task_cell"
    key = os.urandom(32)
    port = _free_port()
    materializer.materialize_task(task_dir, seed=42, fault_mode=fault, persistence=persistence, is_clean_twin=is_clean_twin, evidence_key=key, port=port)

    record = json.loads((task_dir / "tests/fixtures/fault_record.json").read_text())

    out_dir = tmp_path / "app_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_state = tmp_path / "private_state.json"

    server_path = task_dir / "environment/mcp-server/server.py"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "MCP_RECOVERY_OUTPUT": str(out_dir),
        "MCP_RECOVERY_PRIVATE_STATE": str(priv_state),
        "MCP_RECOVERY_HOST": "127.0.0.1",
        "MCP_RECOVERY_PORT": str(port),
    }
    proc = subprocess.Popen([sys.executable, str(server_path)], env=env, cwd=str(task_dir / "environment/mcp-server"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(port)
    except Exception:
        proc.kill()
        raise
    return proc, out_dir, port, key, record


def _client(port: int):
    client = load("client")
    session = client.McpHttpSession(host="127.0.0.1", port=port)
    assert session.initialize()[0] == 200
    return session


def _run_strategy(port: int, strategy: str) -> None:
    session = _client(port)
    key, value = "op-key", "op-val"
    for _ in range(16):
        _status, kind, _raw = session.call_tool("write_record", {"key": key, "value": value})
        if kind != "ok":
            session.call_tool(strategy, {"scope": "default"} if strategy == "refresh_auth" else {"query": key})
            continue
        _status, _kind, raw = session.call_tool("read_record", {"key": key})
        if value in raw and "corrupted_stale_cache_record" not in raw:
            return
        session.call_tool(strategy, {"scope": "default"} if strategy == "refresh_auth" else {"query": key})
    raise AssertionError("Strategy did not recover")


def _run_blind_retry(port: int) -> None:
    session = _client(port)
    for _ in range(8):
        session.call_tool("write_record", {"key": "op-key", "value": "op-val"})
        session.call_tool("read_record", {"key": "op-key"})


@pytest.mark.parametrize("fault", list(FaultClass))
@pytest.mark.parametrize("persistence", [1, 2])
def test_live_fastmcp_fault_recovery_and_blind_retry_gate(tmp_path, fault, persistence):
    pytest.importorskip("fastmcp")
    verifier = load("verifier")

    # 1. Oracle recovery
    proc, out_dir, port, key, record = _start_fastmcp(tmp_path / "oracle", fault, persistence, is_clean_twin=False)
    try:
        _run_strategy(port, "fallback_query")
        envelope_file = out_dir / "sealed-evidence.json"
        assert envelope_file.is_file()
        raw_env = json.loads(envelope_file.read_text())

        scored = verifier.score_recovery_envelope(
            key,
            raw_env,
            task_id=record["task_id"],
            fault_id=record["fault_id"],
            expected_persistence=persistence,
            is_clean_twin=False,
        )
        assert scored["reward"] == 1.0, scored
        assert scored["fault_count"] == persistence, scored
        assert scored["causal_mutation"] is True
        assert scored["auto_clear"] is False
    finally:
        proc.kill()
        proc.wait(timeout=5)

    # 2. Blind retry mutant fails
    proc, out_dir, port, key, record = _start_fastmcp(tmp_path / "blind", fault, persistence, is_clean_twin=False)
    try:
        _run_blind_retry(port)
        envelope_file = out_dir / "sealed-evidence.json"
        raw_env = json.loads(envelope_file.read_text())
        scored = verifier.score_recovery_envelope(
            key,
            raw_env,
            task_id=record["task_id"],
            fault_id=record["fault_id"],
            expected_persistence=persistence,
            is_clean_twin=False,
        )
        assert scored["reward"] == 0.0, scored
        if fault in (FaultClass.TRANSIENT_HTTP_5XX, FaultClass.TRANSIENT_NETWORK_TIMEOUT):
            assert scored["auto_clear"] is True, scored
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_live_fastmcp_clean_twin_recovery(tmp_path):
    pytest.importorskip("fastmcp")
    verifier = load("verifier")

    proc, out_dir, port, key, record = _start_fastmcp(tmp_path / "clean", FaultClass.PERSISTENT_SIGNATURE_ERROR, 2, is_clean_twin=True)
    try:
        _run_strategy(port, "fallback_query")
        envelope_file = out_dir / "sealed-evidence.json"
        raw_env = json.loads(envelope_file.read_text())
        scored = verifier.score_recovery_envelope(
            key,
            raw_env,
            task_id=record["task_id"],
            fault_id=record["fault_id"],
            expected_persistence=0,
            is_clean_twin=True,
        )
        assert scored["reward"] == 1.0, scored
        assert scored["zero_faults"] is True
        assert scored["fault_count"] == 0
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_fixed_policy_evidence_loader_requires_one_bound_trial(tmp_path):
    materializer = load("materializer")
    from evallab.registry import harbor_task_digest

    task = materializer.materialize_task(tmp_path / "task", seed=42, evidence_key=os.urandom(32))
    digest = harbor_task_digest(task)

    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-29T00:00:00Z"}),
        encoding="utf-8",
    )
    (job / "lock.json").write_text("{}", encoding="utf-8")
    (job / "config.json").write_text("{}", encoding="utf-8")
    (job / "lab-metadata.json").write_text("{}", encoding="utf-8")

    trial = job / "trial-one"
    trial.mkdir(parents=True)
    (trial / "lock.json").write_text(json.dumps({"task": {"digest": digest}}), encoding="utf-8")
    (trial / "config.json").write_text("{}", encoding="utf-8")
    (trial / "result.json").write_text(
        json.dumps({"task_name": task.name, "trial_name": "trial-one", "verifier_result": {"rewards": {"reward": 0.0}}}),
        encoding="utf-8",
    )

    fixed_policy = load("fixed_policy")
    evidence = fixed_policy.load_fixed_policy_evidence(job, task)
    assert evidence.reward == 0.0
    assert evidence.task_digest == digest
    assert evidence.trial_dir == trial

    # A second trial is rejected rather than silently selecting arbitrary evidence.
    duplicate = job / "trial-two"
    duplicate.mkdir()
    (duplicate / "lock.json").write_text(json.dumps({"task": {"digest": digest}}), encoding="utf-8")
    (duplicate / "config.json").write_text("{}", encoding="utf-8")
    (duplicate / "result.json").write_text(
        json.dumps({"task_name": task.name, "trial_name": "trial-two", "verifier_result": {"rewards": {"reward": 0.0}}}),
        encoding="utf-8",
    )
    (job / "result.json").write_text(
        json.dumps({"n_total_trials": 2, "stats": {}, "finished_at": "2026-08-29T00:00:00Z"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly one settled trial"):
        fixed_policy.load_fixed_policy_evidence(job, task)

    # Missing/null finished_at cannot be treated as settled evidence.
    (job / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": None}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Not a completed Harbor job"):
        fixed_policy.load_fixed_policy_evidence(job, task)

    # Boolean is not an acceptable numeric reward.
    (job / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-29T00:00:00Z"}),
        encoding="utf-8",
    )
    (duplicate / "lock.json").unlink()
    (duplicate / "config.json").unlink()
    (duplicate / "result.json").unlink()
    duplicate.rmdir()
    trial_result = {"task_name": task.name, "trial_name": "trial-one", "verifier_result": {"rewards": {"reward": True}}}
    (trial / "result.json").write_text(json.dumps(trial_result), encoding="utf-8")
    with pytest.raises(ValueError, match="non-boolean numeric"):
        fixed_policy.load_fixed_policy_evidence(job, task)


def test_retained_evidence_archive_allows_canonical_reconstruction(tmp_path):
    materializer = load("materializer")
    from evallab.registry import discover_control_evidence, harbor_task_digest

    task = materializer.materialize_task(tmp_path / "task", seed=42, evidence_key=os.urandom(32))
    digest = harbor_task_digest(task)

    # Simulate retained evidence tree containing only result.json, lock.json, config.json, lab-metadata.json, reward files
    runs_dir = tmp_path / "research" / "evidence" / "runs"
    for agent_name, reward_val in [("oracle", 1.0), ("nop", 0.0)]:
        job = runs_dir / f"job-{agent_name}"
        job.mkdir(parents=True)
        (job / "result.json").write_text(json.dumps({"n_total_trials": 1, "stats": {}, "finished_at": "2026-08-29T00:00:00Z"}), encoding="utf-8")
        (job / "lock.json").write_text("{}", encoding="utf-8")
        (job / "config.json").write_text("{}", encoding="utf-8")
        (job / "lab-metadata.json").write_text("{}", encoding="utf-8")

        trial = job / f"trial-{agent_name}"
        trial.mkdir()
        (trial / "lock.json").write_text(json.dumps({"task": {"digest": digest, "name": task.name, "version": "1.0.0", "type": "local"}, "agent": {"name": agent_name}}), encoding="utf-8")
        (trial / "config.json").write_text("{}", encoding="utf-8")
        res_data = {
            "task_name": f"local-lab/{task.name}",
            "trial_name": trial.name,
            "task_id": {"path": str(task)},
            "config": {"task": {"path": str(task)}},
            "agent_info": {"name": agent_name},
            "finished_at": "2026-08-29T00:00:00Z",
            "verifier_result": {"rewards": {"reward": reward_val}},
        }
        (trial / "result.json").write_text(json.dumps(res_data), encoding="utf-8")
        (trial / "reward.txt").write_text(f"{reward_val:.1f}\n", encoding="utf-8")

    # Discover control evidence succeeds without needing wheelhouse or build context
    control_evidence = discover_control_evidence(task, tmp_path)
    assert control_evidence.oracle.reward == 1.0
    assert control_evidence.nop.reward == 0.0

    # Fixed-policy loader also succeeds on retained trial
    fixed_policy = load("fixed_policy")
    fp_evidence = fixed_policy.load_fixed_policy_evidence(runs_dir / "job-oracle", task)
    assert fp_evidence.reward == 1.0
    assert fp_evidence.task_digest == digest

def test_all_20_campaign0_cells_materialize_and_pass_workbench_static(monkeypatch):
    wheelhouse = Path("/tmp/mcp-recovery-linux-wheelhouse")
    provenance = Path("/tmp/mcp-recovery-linux-resolver-provenance.json")
    if not wheelhouse.is_dir() or not provenance.is_file():
        pytest.skip("trusted-manifest-bound production wheelhouse unavailable locally; Linux CI certifies static packages")
    provenance_data = json.loads(provenance.read_text(encoding="utf-8"))
    if "manifest_digest" not in provenance_data or "manifest_source" not in provenance_data:
        pytest.skip("local wheelhouse provenance predates trusted-manifest schema; Linux CI regenerates it")
    monkeypatch.setenv("MCP_RECOVERY_WHEELHOUSE", str(wheelhouse))
    monkeypatch.setenv("MCP_RECOVERY_RESOLVER_PROVENANCE", str(provenance))
    materializer = load("materializer")
    repo_root = Path(__file__).resolve().parents[1]
    paths = materializer.materialize_all_campaign0(seed=42)
    assert len(paths) == 20
    source = CandidateSource("https://github.com/PeterMakhnatch/eval-lab", "local/mcp-recovery@1.0.0", "MIT", "03-synthetic")
    for path in paths:
        inspection = inspect_candidate(repo_root=repo_root, task_path=path, source=source)
        assert inspection.static_passed is True, f"{path.name}: {inspection.diagnostics}"


def test_runtime_state_module_not_shadowed_by_sibling_benchmark(monkeypatch):
    """mcp-recovery-v1's `state` import must not be shadowed by loca-lean's `state`.

    Mirrors the CI Python 3.12 cross-benchmark collision: once a sibling benchmark
    has cached its own `state`/`source` modules under the shared bare names, the
    mcp-recovery runtime (which imports `from state import ...`) must still resolve
    this benchmark's modules.
    """
    loca_root = Path(__file__).resolve().parents[1] / "library" / "benchmarks" / "loca-lean-v1"
    monkeypatch.syspath_prepend(str(loca_root))
    state_spec = importlib.util.spec_from_file_location(
        "loca_lean_state_probe", loca_root / "state.py"
    )
    assert state_spec is not None and state_spec.loader is not None
    loca_state = importlib.util.module_from_spec(state_spec)
    sys.modules["loca_lean_state_probe"] = loca_state
    state_spec.loader.exec_module(loca_state)
    # Deterministically cache loca-lean's state.py under the shared bare `state`.
    sys.modules["state"] = loca_state
    assert Path(sys.modules["state"].__file__).resolve() == loca_root.resolve() / "state.py"

    runtime = load("runtime")
    assert runtime.DatabaseState is not None
    assert runtime.compute_digest is not None
    # The displaced sibling `state` module is restored, not permanently clobbered.
    assert sys.modules["state"] is loca_state
