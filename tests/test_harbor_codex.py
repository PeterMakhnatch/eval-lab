"""PinnedCodex tests: the Harbor Codex adapter must keep its CLI version pin.

``harbor.agents.installed.codex`` is stubbed the same way
``tests/test_harbor_zai_miniswe.py`` stubs the mini-swe-agent modules, so these
tests run with or without the optional Harbor dependency installed.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest


class _Codex:
    def __init__(self, *args: Any, version: str | None = None, **kwargs: Any) -> None:
        self.args = args
        self.version = version
        self.kwargs = kwargs


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


@pytest.fixture
def codex_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for name in ("harbor", "harbor.agents", "harbor.agents.installed"):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.codex",
        _module("harbor.agents.installed.codex", Codex=_Codex),
    )
    sys.modules.pop("evallab.harbor_codex", None)
    try:
        yield importlib.import_module("evallab.harbor_codex")
    finally:
        sys.modules.pop("evallab.harbor_codex", None)


def test_pinned_codex_defaults_to_pinned_version(codex_module: ModuleType) -> None:
    agent = codex_module.PinnedCodex()
    assert agent.version == "0.162.1"
    assert agent.version == codex_module.PINNED_CODEX_VERSION


def test_pinned_codex_none_version_uses_pin(codex_module: ModuleType) -> None:
    agent = codex_module.PinnedCodex(version=None)
    assert agent.version == codex_module.PINNED_CODEX_VERSION


def test_pinned_codex_explicit_version_wins(codex_module: ModuleType) -> None:
    agent = codex_module.PinnedCodex(version="9.9.9")
    assert agent.version == "9.9.9"


def test_pinned_codex_forwards_other_arguments(codex_module: ModuleType) -> None:
    agent = codex_module.PinnedCodex("task-1", model_name="glm-5.3-flash")
    assert agent.args == ("task-1",)
    assert agent.kwargs == {"model_name": "glm-5.3-flash"}
    assert agent.version == codex_module.PINNED_CODEX_VERSION
