"""LockedModalEnvironment egress-lock record: applied evidence, not intent.

The record is written from the sandbox creation result and from policy
applications Modal accepted. ``applied`` is true only when an observed
policy is restrictive (no-network or an allowlist with the exact host);
a public policy records ``applied: false``, and a later open policy
never downgrades an applied record.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _install_modal_stub() -> None:
    if "harbor.environments.modal" in sys.modules:
        return
    try:
        importlib.import_module("harbor.environments.modal")
        return
    except ModuleNotFoundError as error:
        if error.name != "harbor" and not (error.name or "").startswith("harbor."):
            raise
    harbor = sys.modules.get("harbor") or types.ModuleType("harbor")
    envs = sys.modules.get("harbor.environments") or types.ModuleType(
        "harbor.environments"
    )
    modal_mod = types.ModuleType("harbor.environments.modal")

    class ModalEnvironment:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.logger = logging.getLogger("test-modal")
            self.trial_paths = SimpleNamespace(trial_dir=Path("."))
            self.session_id = "sess"
            self.network_policy = SimpleNamespace(
                network_mode="public", allowed_hosts=[]
            )
            self._sandbox: Any = None

        async def _create_sandbox(self, **kwargs: Any) -> Any:
            self._sandbox = SimpleNamespace(object_id="sb-test")
            return self._sandbox

        async def _apply_network_policy(self, network_policy: Any) -> None:
            return None

    modal_mod.ModalEnvironment = ModalEnvironment  # type: ignore[attr-defined]
    harbor.environments = envs  # type: ignore[attr-defined]
    envs.modal = modal_mod  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": envs,
            "harbor.environments.modal": modal_mod,
        }
    )


_install_modal_stub()

from evallab.harbor_modal import LockedModalEnvironment  # noqa: E402


def _env(tmp_path: Path) -> LockedModalEnvironment:
    env = LockedModalEnvironment.__new__(LockedModalEnvironment)
    env._evallab_egress_lock = True
    env.trial_paths = SimpleNamespace(trial_dir=tmp_path)
    env.session_id = "sess-1"
    env.logger = logging.getLogger("test-modal")
    env._sandbox = SimpleNamespace(object_id="sb-123")
    return env


def _policy(mode: str, hosts: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(network_mode=mode, allowed_hosts=hosts or [])


def _record(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "egress-lock.json").read_text())


def test_public_creation_records_open_not_applied(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_policy(source="creation", policy=_policy("public"), sandbox=env._sandbox)
    record = _record(tmp_path)
    assert record["requested"] is True
    assert record["applied"] is False
    assert record["mechanism"] is None
    assert record["sandbox_id"] == "sb-123"
    assert record["network_mode"] == "public"


def test_allowlist_application_records_applied_with_exact_host(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    env._record_policy(source="creation", policy=_policy("public"), sandbox=env._sandbox)
    env._record_policy(
        source="apply",
        policy=_policy("allowlist", ["trainer.example"]),
        sandbox=env._sandbox,
    )
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["mechanism"] == "modal-agent-phase-outbound-allowlist"
    assert record["allowed_hosts"] == ["trainer.example"]
    assert record["network_block_all"] is False
    assert record["locked_at"] is not None
    assert [a["source"] for a in record["applications"]] == ["creation", "apply"]


def test_no_network_baseline_records_block_all(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_policy(
        source="creation", policy=_policy("no-network"), sandbox=env._sandbox
    )
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["network_block_all"] is True


def test_open_policy_never_downgrades_applied_record(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_policy(
        source="apply",
        policy=_policy("allowlist", ["trainer.example"]),
        sandbox=env._sandbox,
    )
    env._record_policy(source="apply", policy=_policy("public"), sandbox=env._sandbox)
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["allowed_hosts"] == ["trainer.example"]


def test_apply_wrapper_records_after_modal_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    seen: list[Any] = []

    async def fake_apply(self: Any, network_policy: Any) -> None:
        seen.append(network_policy)

    monkeypatch.setattr(
        "evallab.harbor_modal.ModalEnvironment._apply_network_policy",
        fake_apply,
    )
    asyncio.run(env._apply_network_policy(_policy("allowlist", ["trainer.example"])))
    assert len(seen) == 1
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["sandbox_id"] == "sb-123"
