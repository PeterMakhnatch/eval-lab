from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

BENCH_ROOT = Path(__file__).parents[1] / "library" / "benchmarks" / "mcp-funcdag-v1"


def _load_module(name: str):
    module_name = f"mcp_funcdag_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    orig_path = list(sys.path)
    sys.path.insert(0, str(BENCH_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(module_name, BENCH_ROOT / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = orig_path
        for generic_name in ("contract", "dag_generator", "materializer", "runtime", "templates", "verifier"):
            mod = sys.modules.get(generic_name)
            if mod is not None and getattr(mod, "__file__", "").startswith(str(BENCH_ROOT)):
                del sys.modules[generic_name]


def _load_ensure_wheelhouse_module():
    spec = importlib.util.spec_from_file_location(
        "mcp_funcdag_ensure_wheelhouse",
        Path(__file__).parents[1] / "scripts" / "mcp_funcdag" / "ensure_wheelhouse.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dag_generator_determinism():
    dag_gen = _load_module("dag_generator")
    spec1 = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    spec2 = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    assert spec1.target_node_id == spec2.target_node_id
    assert spec1.expected_target_value == spec2.expected_target_value
    assert spec1.topological_order == spec2.topological_order
    assert len(spec1.nodes) >= 5
    assert len(spec1.node_expected_calls) == len(spec1.nodes)


def test_dag_generator_minimum_floor_enforcement():
    dag_gen = _load_module("dag_generator")
    with pytest.raises(ValueError, match="below mandatory floor"):
        dag_gen.generate_dag_spec(seed=42, depth=2, width=2, distractor_count=2)


def test_benchmark_contract_and_campaign_cells():
    contract_mod = _load_module("contract")
    dag_gen = _load_module("dag_generator")
    factors = contract_mod.CellFactors(depth=3, width=2, distractor_count=2)
    spec = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=2)
    contract = contract_mod.make_benchmark_contract(factors, spec, "test-task-1")
    assert contract.family == "mcp-funcdag-v1"
    assert contract.opportunity_counts["required_node_count"] >= 5
    assert len(contract_mod.CAMPAIGN_0_CELLS) == 30
    assert "saturation_state" not in contract.to_dict()
    assert contract.artifact_paths["result"] == "/app/result.json"
    assert contract.artifact_paths["events"] == "/app/output/benchmark-events.jsonl"


def test_streamable_mcp_runtime_and_events(tmp_path):
    dag_gen = _load_module("dag_generator")
    runtime_mod = _load_module("runtime")
    spec = dag_gen.generate_dag_spec(seed=42, depth=3, width=2, distractor_count=1)
    spec_dict = {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {
                        "name": p.name,
                        "type_name": p.type_name,
                        "description": p.description,
                        "required": p.required,
                    }
                    for p in t.parameters
                ],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
                "op_kind": t.op_kind,
            }
            for t in spec.tools
        ],
        "nodes": [
            {
                "node_id": n.node_id,
                "tool_name": n.tool_name,
                "op_name": n.op_name,
                "input_bindings": n.input_bindings,
            }
            for n in spec.nodes
        ],
        "initial_inputs": spec.initial_inputs,
        "target_node_id": spec.target_node_id,
        "topological_order": spec.topological_order,
    }
    runtime = runtime_mod.MCPRuntime(spec_dict, tmp_path)
    target_node = spec.nodes[0]
    args = {k: spec.initial_inputs[src] for k, src in target_node.input_bindings.items()}
    out = runtime.call_tool(target_node.tool_name, args)
    assert out["result"]["value"] == spec.reference_node_values[target_node.node_id]
    runtime.call_tool(target_node.tool_name, args)
    assert runtime.redundant_calls == 1
    different_args = {k: (v + 1 if isinstance(v, int) else v) for k, v in args.items()}
    runtime.call_tool(target_node.tool_name, different_args)
    assert runtime.redundant_calls == 1


def test_materializer_uses_fastmcp_substrate(tmp_path):
    contract_mod = _load_module("contract")
    materializer_mod = _load_module("materializer")
    cell = contract_mod.CAMPAIGN_0_CELLS[0]
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path)
    sidecar = task_dir / "environment" / "mcp-server"
    server_py = (sidecar / "server.py").read_text(encoding="utf-8")
    assert "from fastmcp import FastMCP" in server_py
    assert 'transport="streamable-http"' in server_py
    oracle_py = (task_dir / "solution" / "solve.py").read_text(encoding="utf-8")
    assert "class McpHttpSession:" in oracle_py
    assert "session = McpHttpSession(" in oracle_py
    assert 'MCP_HOST = "mcp-service"' in oracle_py
    assert 'result_path = Path("/app/result.json")' in oracle_py
    # The generated oracle must be valid, executable Python: a template
    # escaping defect here (e.g. a mis-escaped newline) silently scores the
    # Docker oracle 0.0 in the Linux certification gate.
    compile(oracle_py, "solve.py", "exec")
    assert not (sidecar / "runtime.py").exists()
    compose = (task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "mcp-service:" in compose
    assert "workbench-internal:" in compose
    assert "internal: true" in compose
    assert "evidence-volume:" in compose
    task_toml = (task_dir / "task.toml").read_text(encoding="utf-8")
    assert 'transport = "streamable-http"' in task_toml
    assert "http://mcp-service:8080/mcp" in task_toml
    assert '"/app/result.json"' in task_toml
    assert "[[verifier.collect]]" not in task_toml
    verifier_eval = (task_dir / "tests" / "verifier_eval.py").read_text(encoding="utf-8")
    assert 'res_file = Path("/app/result.json")' in verifier_eval
    compile(verifier_eval, "verifier_eval.py", "exec")
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    assert "Dependency Graph Nodes:" in instruction
    assert "transformation" in instruction
    assert "uses tool" not in instruction  # No gold tool mapping leak in instruction
    assert "/app/result.json" in instruction


def test_materializer_oracle_nop_mutants_and_answer_only(tmp_path):
    contract_mod = _load_module("contract")
    materializer_mod = _load_module("materializer")
    runtime_mod = _load_module("runtime")
    templates_mod = _load_module("templates")
    verifier_mod = _load_module("verifier")
    cell = contract_mod.CAMPAIGN_0_CELLS[0]
    task_dir = materializer_mod.materialize_task(cell, output_root=tmp_path)
    spec_data = json.loads((task_dir / "environment" / "runtime_tools.json").read_text())
    truth_path = task_dir / "tests" / "fixtures" / "verifier_truth.json"
    evidence_dir = tmp_path / "evidence-oracle"
    workspace_dir = tmp_path / "workspace-oracle"
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_dir)
    templates_mod.run_oracle_solve(runtime, spec_data, workspace_dir)
    res_oracle = verifier_mod.verify_execution(task_dir, truth_path, evidence_dir, workspace_dir)
    assert res_oracle["reward"] == 1.0
    assert res_oracle["dag_conformance"] is True
    assert res_oracle["value_propagation_accuracy"] == 1.0
    assert res_oracle["contiguous_ordinals"] is True

    evidence_nop = tmp_path / "evidence-nop"
    workspace_nop = tmp_path / "workspace-nop"
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_nop)
    templates_mod.run_nop_solve(runtime, spec_data, workspace_nop)
    assert verifier_mod.verify_execution(task_dir, truth_path, evidence_nop, workspace_nop)["reward"] == 0.0

    evidence_ao = tmp_path / "evidence-ao"
    workspace_ao = tmp_path / "workspace-ao"
    workspace_ao.mkdir()
    truth = json.loads(truth_path.read_text())
    (workspace_ao / "result.json").write_text(json.dumps({"target_value": truth["expected_target_value"]}))
    runtime = runtime_mod.MCPRuntime(spec_data, evidence_ao)
    assert verifier_mod.verify_execution(task_dir, truth_path, evidence_ao, workspace_ao)["reward"] == 0.0

    for mname, mfn in templates_mod.get_mutants().items():
        evidence_m = tmp_path / f"evidence-{mname}"
        workspace_m = tmp_path / f"workspace-{mname}"
        runtime = runtime_mod.MCPRuntime(spec_data, evidence_m)
        mfn(runtime, spec_data, workspace_m)
        res = verifier_mod.verify_execution(task_dir, truth_path, evidence_m, workspace_m)
        assert res["reward"] == 0.0, f"Mutant {mname} did not score 0.0"


def test_coexistence_with_loca_lean():
    loca_test_path = Path(__file__).parents[1] / "tests" / "test_loca_lean.py"
    spec = importlib.util.spec_from_file_location("test_loca_lean_isolated", loca_test_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    loca_source = mod.load("loca_source_coexist", "source")
    assert loca_source is not None


def test_ensure_wheelhouse_uses_locked_resolver_not_live_uv_pip(tmp_path, monkeypatch):
    """Staging runs the locked environment's python -m pip, never a live `uv run --with pip`."""
    mod = _load_ensure_wheelhouse_module()

    dest = tmp_path / "wheels"
    target = mod.LINUX_CP312_X86_64
    cmd = mod.stage_command(dest, target)
    # The resolver is the locked environment's pip executed via sys.executable,
    # not a live pip resolved by `uv run --with pip`.
    assert cmd[0] == sys.executable
    assert cmd[1:4] == ["-m", "pip", "download"]
    assert "--platform" in cmd
    assert cmd[cmd.index("--platform") + 1] == "manylinux_2_17_x86_64"
    assert "--require-hashes" not in cmd
    assert "uv" not in cmd[:4]

    from evallab.mcp_substrate import (
        ResolverProvenance,
        render_provenance_lock,
        trusted_wheel_manifest_digest,
        trusted_wheel_manifest_source,
    )

    provenance = ResolverProvenance(
        target=target,
        manifest_digest=trusted_wheel_manifest_digest(),
        manifest_source=trusted_wheel_manifest_source(),
        wheels=(
            {
                "filename": "fastmcp-3.4.7-py3-none-any.whl",
                "name": "fastmcp",
                "version": "3.4.7",
                "size_bytes": 8016,
                "sha256": "a" * 64,
            },
        ),
    )
    monkeypatch.setattr(mod, "record_prepackaging_provenance", lambda *_: provenance)
    recorded: list[list[str]] = []

    def fake_run(argv, check=False):
        recorded.append(list(argv))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "fastmcp-3.4.7-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(argv, 0)

    observed = mod.ensure_wheelhouse(dest, target=target, run=fake_run)
    assert observed == provenance
    assert recorded and recorded[0] == cmd
    stored = json.loads((dest / mod.PROVENANCE_FILENAME).read_text(encoding="utf-8"))
    assert stored == provenance.to_dict()
    provenance_lock = render_provenance_lock(provenance)
    assert "fastmcp==3.4.7 --hash=sha256:" in provenance_lock


def test_stage_command_pins_full_trusted_manifest_inventory():
    """The trusted Linux download command must request every manifest name==version pin."""
    from evallab.mcp_substrate import load_trusted_wheel_manifest

    mod = _load_ensure_wheelhouse_module()
    manifest = load_trusted_wheel_manifest()
    expected = {f"{entry['name']}=={entry['version']}" for entry in manifest["wheels"]}
    assert len(expected) == 68

    cmd = mod.stage_command(Path("/tmp/nonexistent-wheels"), mod.LINUX_CP312_X86_64)
    pins = cmd[cmd.index("--dest") + 2 :]
    assert set(pins) == expected
    # The exact reviewed transitive pins are present (not merely fastmcp), so
    # resolver drift cannot silently upgrade a dependency.
    assert "joserfc==1.7.4" in pins
    assert "fastmcp==3.4.7" in pins
    assert "cffi==2.1.1" in pins


def test_stage_command_rejects_non_trusted_target():
    """A non-trusted target with no reviewed manifest is refused explicitly."""
    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    with pytest.raises(SubstrateError, match="no trusted wheel manifest"):
        mod.stage_command(Path("/tmp/wheels"), mod.MACOS_CP312_ARM64)


def test_resolver_pip_is_pinned_in_locked_dev_group():
    """The resolver pip must be pinned exactly in the locked dev dependency group."""
    import tomllib

    mod = _load_ensure_wheelhouse_module()
    pyproject_path = Path(__file__).parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    dev = data["dependency-groups"]["dev"]
    assert mod.RESOLVER_PIP_PIN in dev
    # The lock must carry the exact pinned pip with a committed artifact hash.
    lock_text = (Path(__file__).parents[1] / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "pip"' in lock_text
    assert 'version = "26.1.2"' in lock_text
    assert "pip-26.1.2" in lock_text


def test_ensure_wheelhouse_rejects_non_trusted_target_before_cache(tmp_path):
    """A crafted cached provenance must not bypass rejection of a non-trusted target."""
    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)
    # Plant a provenance claiming the non-trusted target with a wheel present.
    (dest / mod.PROVENANCE_FILENAME).write_text(
        json.dumps(
            {
                "target": {"python_tag": "cp312", "platform_tag": "macosx_11_0_arm64"},
                "manifest_digest": "0" * 64,
                "manifest_source": "https://pypi.org/simple",
                "wheels": [],
            }
        ),
        encoding="utf-8",
    )
    (dest / "fastmcp-3.4.7-py3-none-any.whl").write_bytes(b"wheel")
    recorded: list[list[str]] = []

    def fake_run(argv, check=False):
        recorded.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(SubstrateError, match="no trusted wheel manifest"):
        mod.ensure_wheelhouse(dest, target=mod.MACOS_CP312_ARM64, run=fake_run)
    assert not recorded, "non-trusted target must be rejected before any staging"


def test_ensure_wheelhouse_rejects_wrong_pip_resolver_version(tmp_path, monkeypatch):
    """A pip version other than the pinned locked resolver is refused before staging."""
    import importlib.metadata as metadata

    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)

    real_version = metadata.version("pip")
    assert real_version == mod.RESOLVER_PIP_PIN.split("==", 1)[1]
    monkeypatch.setattr(
        "importlib.metadata.version", lambda dist: "9.9.9" if dist == "pip" else real_version
    )

    def fake_run(argv, check=False):
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(SubstrateError, match="resolver pip version mismatch"):
        mod.ensure_wheelhouse(dest, target=mod.LINUX_CP312_X86_64, run=fake_run)
    with pytest.raises(SubstrateError, match="resolver pip version mismatch"):
        mod.stage_command(dest, mod.LINUX_CP312_X86_64)


def test_ensure_wheelhouse_rejects_missing_pip_resolver(tmp_path, monkeypatch):
    """A missing pip resolver is refused fail-closed before any staging."""
    import importlib.metadata as metadata

    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)

    def fake_version(dist):
        if dist == "pip":
            raise metadata.PackageNotFoundError("pip")
        return metadata.version(dist)

    monkeypatch.setattr("importlib.metadata.version", fake_version)

    def fake_run(argv, check=False):
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(SubstrateError, match="not installed"):
        mod.ensure_wheelhouse(dest, target=mod.LINUX_CP312_X86_64, run=fake_run)


def test_ensure_wheelhouse_destination_creation_race_revalidates(tmp_path, monkeypatch):
    """A FileExistsError during atomic creation re-validates the raced path, not chmods blindly."""
    from evallab.mcp_substrate import (
        ResolverProvenance,
        trusted_wheel_manifest_digest,
        trusted_wheel_manifest_source,
    )

    mod = _load_ensure_wheelhouse_module()
    target = mod.LINUX_CP312_X86_64
    dest = tmp_path / "wheels"

    provenance = ResolverProvenance(
        target=target,
        manifest_digest=trusted_wheel_manifest_digest(),
        manifest_source=trusted_wheel_manifest_source(),
        wheels=(),
    )
    monkeypatch.setattr(mod, "record_prepackaging_provenance", lambda *_: provenance)

    real_makedirs = mod.os.makedirs

    def racy_makedirs(path, mode=0o777, exist_ok=False):
        if str(path).endswith("wheels") and not exist_ok:
            # First call races: another actor created the dir after our lstat miss.
            Path(path).mkdir(parents=True, exist_ok=True)
            raise FileExistsError(f"race: {path}")
        return real_makedirs(path, mode=mode, exist_ok=exist_ok)

    monkeypatch.setattr(mod.os, "makedirs", racy_makedirs)

    def fake_run(argv, check=False):
        return subprocess.CompletedProcess(argv, 0)

    mod.ensure_wheelhouse(dest, target=target, run=fake_run)
    # The raced existing dir was validated (owner-only not required post-race;
    # it is our own tmp_path dir), not chmodded by the helper.
    assert dest.is_dir()
    assert not dest.is_symlink()


def test_ensure_wheelhouse_rejects_symlinked_destination(tmp_path):
    """A symlinked destination is refused before any cache reuse/clear/write."""
    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "dest"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SubstrateError, match="symlink"):
        mod.ensure_wheelhouse(link, target=mod.LINUX_CP312_X86_64)


def test_ensure_wheelhouse_rejects_other_owned_destination(tmp_path, monkeypatch):
    """An existing destination owned by another uid is refused before deletion/download/write."""
    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)
    # Simulate an attacker-owned directory without needing root to chown: report a
    # different effective uid so the ownership check must fail closed.
    real_euid = mod.os.geteuid()
    monkeypatch.setattr(mod.os, "geteuid", lambda: real_euid + 1)
    with pytest.raises(SubstrateError, match="not owned by the current user"):
        mod.ensure_wheelhouse(dest, target=mod.LINUX_CP312_X86_64)


def test_ensure_wheelhouse_rejects_group_or_world_writable_destination(tmp_path):
    """A real but group/world-writable destination is refused before deletion/download/write."""
    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o777)
    with pytest.raises(SubstrateError, match="group/world-writable"):
        mod.ensure_wheelhouse(dest, target=mod.LINUX_CP312_X86_64)


def test_ensure_wheelhouse_creates_absent_destination_owner_only(tmp_path, monkeypatch):
    """An absent destination is created with owner-only permissions before staging."""
    from evallab.mcp_substrate import (
        ResolverProvenance,
        trusted_wheel_manifest_digest,
        trusted_wheel_manifest_source,
    )

    mod = _load_ensure_wheelhouse_module()
    target = mod.LINUX_CP312_X86_64
    dest = tmp_path / "wheels"

    provenance = ResolverProvenance(
        target=target,
        manifest_digest=trusted_wheel_manifest_digest(),
        manifest_source=trusted_wheel_manifest_source(),
        wheels=(),
    )
    monkeypatch.setattr(mod, "record_prepackaging_provenance", lambda *_: provenance)

    def fake_run(argv, check=False):
        dest.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0)

    mod.ensure_wheelhouse(dest, target=target, run=fake_run)
    mode = dest.stat().st_mode & 0o777
    assert mode == 0o700


def test_ensure_wheelhouse_rejects_symlinked_provenance(tmp_path):
    """A symlinked resolver-provenance final component is refused before read/write."""
    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "fastmcp-3.4.7-py3-none-any.whl").write_bytes(b"wheel")
    target_file = tmp_path / "target.json"
    target_file.write_text("{}", encoding="utf-8")
    (dest / mod.PROVENANCE_FILENAME).symlink_to(target_file)
    with pytest.raises(SubstrateError, match="symlink"):
        mod.ensure_wheelhouse(dest, target=mod.LINUX_CP312_X86_64)


def test_ensure_wheelhouse_rejects_symlinked_wheel_entry(tmp_path, monkeypatch):
    """A symlinked wheel entry inside an otherwise-valid cache is refused, not followed."""
    from evallab.mcp_substrate import SubstrateError

    mod = _load_ensure_wheelhouse_module()
    target = mod.LINUX_CP312_X86_64
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)

    from evallab.mcp_substrate import (
        ResolverProvenance,
        trusted_wheel_manifest_digest,
        trusted_wheel_manifest_source,
    )

    provenance = ResolverProvenance(
        target=target,
        manifest_digest=trusted_wheel_manifest_digest(),
        manifest_source=trusted_wheel_manifest_source(),
        wheels=(),
    )
    (dest / mod.PROVENANCE_FILENAME).write_text(
        json.dumps(provenance.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.whl"
    outside.write_bytes(b"wheel")
    (dest / "fastmcp-3.4.7-py3-none-any.whl").symlink_to(outside)

    def fake_run(argv, check=False):
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(SubstrateError, match="symlink"):
        mod.ensure_wheelhouse(dest, target=target, run=fake_run)


def test_ensure_wheelhouse_cache_reuse_fails_closed_when_provenance_does_not_verify(
    tmp_path, monkeypatch
):
    """A cached wheelhouse whose provenance does not verify exactly is re-staged, not trusted."""
    from evallab.mcp_substrate import (
        ResolverProvenance,
        trusted_wheel_manifest_digest,
        trusted_wheel_manifest_source,
    )

    mod = _load_ensure_wheelhouse_module()
    target = mod.LINUX_CP312_X86_64
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)
    # Plant a stale provenance claiming the right target but a single bogus wheel
    # that cannot match the 68-wheel trusted manifest.
    stale = ResolverProvenance(
        target=target,
        manifest_digest=trusted_wheel_manifest_digest(),
        manifest_source=trusted_wheel_manifest_source(),
        wheels=(
            {
                "filename": "fastmcp-3.4.7-py3-none-any.whl",
                "name": "fastmcp",
                "version": "3.4.7",
                "size_bytes": 8016,
                "sha256": "a" * 64,
            },
        ),
    )
    (dest / mod.PROVENANCE_FILENAME).write_text(
        json.dumps(stale.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (dest / "fastmcp-3.4.7-py3-none-any.whl").write_bytes(b"wheel")

    fresh = ResolverProvenance(
        target=target,
        manifest_digest=trusted_wheel_manifest_digest(),
        manifest_source=trusted_wheel_manifest_source(),
        wheels=stale.wheels,
    )
    monkeypatch.setattr(mod, "record_prepackaging_provenance", lambda *_: fresh)
    recorded: list[list[str]] = []

    def fake_run(argv, check=False):
        recorded.append(list(argv))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "fastmcp-3.4.7-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(argv, 0)

    observed = mod.ensure_wheelhouse(dest, target=target, run=fake_run)
    # The stale cache was discarded and staging re-run rather than trusted on the
    # target tag alone.
    assert recorded, "expected staging to re-run when cached provenance does not verify"
    assert observed == fresh


def test_ensure_wheelhouse_cache_reuse_keeps_verifying_provenance(tmp_path, monkeypatch):
    """A cached wheelhouse that verifies exactly against the manifest is reused without re-run."""
    mod = _load_ensure_wheelhouse_module()
    target = mod.LINUX_CP312_X86_64
    dest = tmp_path / "wheels"
    dest.mkdir(parents=True, exist_ok=True)

    from evallab.mcp_substrate import (
        ResolverProvenance,
        trusted_wheel_manifest_digest,
        trusted_wheel_manifest_source,
    )

    provenance = ResolverProvenance(
        target=target,
        manifest_digest=trusted_wheel_manifest_digest(),
        manifest_source=trusted_wheel_manifest_source(),
        wheels=(
            {
                "filename": "fastmcp-3.4.7-py3-none-any.whl",
                "name": "fastmcp",
                "version": "3.4.7",
                "size_bytes": 8016,
                "sha256": "a" * 64,
            },
        ),
    )
    (dest / mod.PROVENANCE_FILENAME).write_text(
        json.dumps(provenance.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (dest / "fastmcp-3.4.7-py3-none-any.whl").write_bytes(b"wheel")

    # Simulate a perfectly verified cache: verify_provenance_wheelhouse succeeds.
    monkeypatch.setattr(mod, "verify_provenance_wheelhouse", lambda *_: [])
    recorded: list[list[str]] = []

    def fake_run(argv, check=False):
        recorded.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    observed = mod.ensure_wheelhouse(dest, target=target, run=fake_run)
    assert observed == provenance
    assert not recorded, "a verified cached wheelhouse must not re-run staging"


def test_materializer_main_image_environment(tmp_path):
    wheelhouse = Path("/tmp/fastmcp3_wheelhouse")
    if not (wheelhouse / "resolver-provenance.json").is_file():
        pytest.skip("FastMCP wheelhouse is not staged on this host")
    contract_mod = _load_module("contract")
    materializer_mod = _load_module("materializer")
    task_dir = materializer_mod.materialize_task(
        contract_mod.CAMPAIGN_0_CELLS[0], output_root=tmp_path, wheelhouse=wheelhouse
    )
    dockerfile = (task_dir / "environment" / "Dockerfile").read_text(encoding="utf-8")
    assert "RUN mkdir -p /app /app/output" in dockerfile
    assert "COPY mcp-server" not in dockerfile
