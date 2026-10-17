"""Modal environments: regrade entry contract + egress-lock record.

``RegradeModalEnvironment`` must actually resolve at
``evallab.harbor_modal:RegradeModalEnvironment`` (a string-only test
missed its deletion once) with its PATH-pin and bounded-lifetime
contract. ``LockedModalEnvironment``'s record is written from the
*effective* arguments sent to Modal — an explicit ``block_network``
override wins over the static policy — and from policy applications
Modal accepted. ``applied`` is true only when the effective sent policy
is restrictive; a later open policy never downgrades an applied record.
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

from evallab.execution_contracts import MODAL_PATH_PIN  # noqa: E402
from evallab.harbor_modal import (  # noqa: E402
    LockedModalEnvironment,
    ModalEnvironment,
    RegradeModalEnvironment,
)


# --- RegradeModalEnvironment: the entry must actually import -----------------


def test_regrade_entry_actually_imports_as_modal_subclass() -> None:
    module = importlib.import_module("evallab.harbor_modal")
    resolved = getattr(module, "RegradeModalEnvironment", None)
    assert resolved is not None
    assert issubclass(resolved, ModalEnvironment)


def test_regrade_kwargs_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_init(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(ModalEnvironment, "__init__", fake_init)
    RegradeModalEnvironment(
        sandbox_timeout_secs=86400, persistent_env={"OTHER": "kept"}
    )
    assert captured["sandbox_timeout_secs"] == 3600
    assert captured["persistent_env"] == {"OTHER": "kept", "PATH": MODAL_PATH_PIN}

    captured.clear()
    RegradeModalEnvironment(sandbox_timeout_secs=600)
    assert captured["sandbox_timeout_secs"] == 600
    assert captured["persistent_env"] == {"PATH": MODAL_PATH_PIN}


# --- LockedModalEnvironment: record from effective sent policy ---------------


def _env(tmp_path: Path) -> LockedModalEnvironment:
    env = LockedModalEnvironment.__new__(LockedModalEnvironment)
    env._evallab_egress_lock = True
    env.trial_paths = SimpleNamespace(trial_dir=tmp_path)
    env.session_id = "sess-1"
    env.logger = logging.getLogger("test-modal")
    env._sandbox = SimpleNamespace(object_id="sb-123")
    return env


def _record(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "egress-lock.json").read_text())


def test_public_creation_records_open_not_applied(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_application(
        source="creation", mode="public", hosts=[], sandbox=env._sandbox
    )
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
    env._record_application(
        source="creation", mode="public", hosts=[], sandbox=env._sandbox
    )
    env._record_application(
        source="apply", mode="allowlist", hosts=["trainer.example"], sandbox=env._sandbox
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
    env._record_application(
        source="creation", mode="no-network", hosts=[], sandbox=env._sandbox
    )
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["network_block_all"] is True


def test_open_policy_never_downgrades_applied_record(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_application(
        source="apply", mode="allowlist", hosts=["trainer.example"], sandbox=env._sandbox
    )
    env._record_application(
        source="apply", mode="public", hosts=[], sandbox=env._sandbox
    )
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["allowed_hosts"] == ["trainer.example"]


def test_effective_creation_policy_override_wins() -> None:
    """The block_network override, not the static baseline, is recorded."""
    effective = LockedModalEnvironment._effective_creation_policy
    no_network = SimpleNamespace(
        network_policy=SimpleNamespace(network_mode="no-network", allowed_hosts=[]),
        _dynamic_network=False,
        _network_disabled=True,
        _network_is_allowlist=False,
    )
    # DinD-style override: public networking is sent despite the baseline.
    assert effective(no_network, False) == ("public", [])
    # No override: the no-network baseline is what is sent.
    assert effective(no_network, None) == ("no-network", [])
    assert effective(no_network, True) == ("no-network", [])

    allowlist = SimpleNamespace(
        network_policy=SimpleNamespace(
            network_mode="allowlist", allowed_hosts=["trainer.example"]
        ),
        _dynamic_network=False,
        _network_disabled=False,
        _network_is_allowlist=True,
    )
    assert effective(allowlist, None) == ("allowlist", ["trainer.example"])
    # Dynamic networking sends the startup policy's outbound kwargs.
    dynamic = SimpleNamespace(
        network_policy=SimpleNamespace(
            network_mode="allowlist", allowed_hosts=["trainer.example"]
        ),
        _dynamic_network=True,
        _network_disabled=False,
        _network_is_allowlist=True,
    )
    assert effective(dynamic, False) == ("allowlist", ["trainer.example"])


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
    asyncio.run(
        env._apply_network_policy(
            SimpleNamespace(network_mode="allowlist", allowed_hosts=["trainer.example"])
        )
    )
    assert len(seen) == 1
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["sandbox_id"] == "sb-123"


def _real_harbor_modal() -> bool:
    module = sys.modules.get("harbor.environments.modal")
    return bool(
        module is not None
        and getattr(module, "_HAS_MODAL", False)
        and hasattr(module, "Sandbox")
    )


@pytest.mark.skipif(
    not _real_harbor_modal(),
    reason="requires the real Harbor ModalEnvironment with an inert SDK boundary",
)
def test_real_creation_records_effective_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Harbor creation code, fake Modal SDK: both override states.

    With a static no-network baseline, ``block_network=False`` (Harbor's
    DinD path) sends public networking and must record open; with no
    override the baseline blocks all and must record applied.
    """
    import harbor.environments.modal as modal_mod
    from harbor.models.task.config import NetworkMode, NetworkPolicy

    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(object_id="sb-fake")

    monkeypatch.setattr(
        modal_mod, "Sandbox", SimpleNamespace(create=SimpleNamespace(aio=fake_create))
    )
    for name, value in (
        ("_cpu_config", None),
        ("_memory_config", None),
        ("_gpu_config", None),
        ("_sandbox_labels", {}),
        ("_secrets_config", []),
        ("_volumes_config", {}),
    ):
        monkeypatch.setattr(
            modal_mod.ModalEnvironment, name, lambda self, _v=value: _v
        )
    monkeypatch.setattr(modal_mod.ModalEnvironment, "stream_enabled", False)

    def make_env() -> LockedModalEnvironment:
        env = LockedModalEnvironment.__new__(LockedModalEnvironment)
        env._evallab_egress_lock = True
        env.trial_paths = SimpleNamespace(trial_dir=tmp_path)
        env.session_id = "sess-1"
        env.logger = logging.getLogger("test-modal")
        env._network_policy = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
        env._dynamic_network = False
        env._app = object()
        env._image = object()
        env._sandbox_timeout = 60
        env._sandbox_idle_timeout = None
        env._kwargs = {}
        env._sandbox = None
        return env

    asyncio.run(make_env()._create_sandbox(block_network=False))
    assert captured["block_network"] is False
    record = _record(tmp_path)
    assert record["applied"] is False
    assert record["network_mode"] == "public"
    assert record["sandbox_id"] == "sb-fake"

    (tmp_path / "egress-lock.json").unlink()
    asyncio.run(make_env()._create_sandbox())
    assert captured["block_network"] is True
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["network_block_all"] is True
    assert record["sandbox_id"] == "sb-fake"
