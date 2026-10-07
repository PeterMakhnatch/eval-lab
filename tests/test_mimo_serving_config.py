"""Unsafe resource choices fail before a Modal deployment can be registered."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EVALLAB_MIMO_GPU", "A10"),
        ("EVALLAB_MIMO_GPU", "A100-80GB:2"),
        ("EVALLAB_MIMO_GPU", ""),
        ("EVALLAB_MIMO_MAX_RUNNING_REQUESTS", "0"),
        ("EVALLAB_MIMO_MAX_RUNNING_REQUESTS", "21"),
        ("EVALLAB_MIMO_MAX_RUNNING_REQUESTS", "1.5"),
        ("EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS", "0"),
        ("EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS", "1201"),
        ("EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS", "forever"),
    ],
)
def test_invalid_deployment_configuration_refuses_before_registration(monkeypatch, name, value):
    def decorator(**kwargs):
        return lambda wrapped: wrapped

    modal = Mock()
    modal.App.side_effect = lambda app_name: SimpleNamespace(function=decorator, server=decorator)
    modal.enter = decorator
    modal.exit = decorator
    monkeypatch.setitem(sys.modules, "modal", modal)
    for key in (
        "EVALLAB_MIMO_GPU",
        "EVALLAB_MIMO_MAX_RUNNING_REQUESTS",
        "EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(name, value)
    path = Path(__file__).resolve().parents[1] / "tools/modal-mimo-serve/serve.py"
    spec = importlib.util.spec_from_file_location("har192_invalid_serving", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(ValueError):
        spec.loader.exec_module(module)
    modal.App.assert_not_called()
