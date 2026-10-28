"""Offline admission-stop tests, explicitly NOT an OMP dispatch-denial proof."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent
EXECUTED_SHA = "87c0d0bb6296b28d5a38804fdfc6211e5ad1a61ff8e926f43bf4b8aa811eca28"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_deny_prevents_transport_and_queued_synthetic_tool(tmp_path):
    gate = load_module("tt7_boundary_deny_test", "native_inference_gate.py")
    side_effect = tmp_path / "synthetic-handler-effect"
    counters = {"provider_launches": 0, "handler_invocations": 0}
    queued_event = {"type": "toolCall", "name": "synthetic_local_write"}

    def stateful_handler(event):
        assert event is queued_event
        counters["handler_invocations"] += 1
        side_effect.write_text("synthetic effect")

    def fake_transport_with_unsolicited_tool_call():
        counters["provider_launches"] += 1
        stateful_handler(queued_event)

    with pytest.raises(gate.NativeInferenceBlocked, match="no verified empty dispatch"):
        gate.require_native_boundary(fake_transport_with_unsolicited_tool_call)
    assert counters == {"provider_launches": 0, "handler_invocations": 0}
    assert not side_effect.exists()


def test_forward_cli_blocks_before_archive_or_proof_read(tmp_path, monkeypatch, capsys):
    gate = load_module("tt7_boundary_cli_test", "native_inference_gate.py")
    launches = []
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: launches.append((a, k)))
    untrusted_permission = tmp_path / "nonexistent-fresh-quota-entitlement.json"
    assert gate.main(["--entitlement", str(untrusted_permission), "run"]) == 78
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "BLOCKED"
    assert status["native_generation_allowed"] is False
    assert status["cli_dispatch_denial_proven"] is False
    assert launches == []
    assert not untrusted_permission.exists()


def test_forward_cli_has_no_permission_bypass(monkeypatch):
    gate = load_module("tt7_boundary_no_bypass_test", "native_inference_gate.py")
    launches = []
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: launches.append((a, k)))
    with pytest.raises(SystemExit) as error:
        gate.main(["run", "--allow-native"])
    assert error.value.code == 2
    assert launches == []


def test_archived_executed_driver_stays_exact_and_seal_precedes_quota(tmp_path, monkeypatch):
    source = ROOT / "continue_codex.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == EXECUTED_SHA
    archived = load_module("tt7_archived_sealed_test", "continue_codex.py")
    seal = tmp_path / "attempt.json"
    seal.write_text('{"status":"sealed"}\n')
    monkeypatch.setattr(archived, "RECEIPT", seal)
    monkeypatch.setattr(archived, "bind", lambda: SimpleNamespace(
        common=SimpleNamespace(STATE=tmp_path),
    ))
    monkeypatch.setattr(archived, "configure", lambda _bindings, private: private)

    def forbidden(*_args, **_kwargs):
        pytest.fail("sealed historical runner reached an input, quota or invocation path")

    for name in ("load_json", "entitlement", "privacy_audit", "read_cell"):
        monkeypatch.setattr(archived, name, forbidden)
    with pytest.raises(ValueError, match="attempt receipt is sealed"):
        archived.run(tmp_path / "never-read-proof.json")
