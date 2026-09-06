#!/usr/bin/env python3
"""Shared module loader and paths for MCP FuncDAG scripts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = ROOT / "library" / "benchmarks" / "mcp-funcdag-v1"


def load_benchmark_module(name: str) -> ModuleType:
    """Dynamically load an uninstalled benchmark module by filename."""
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


_load_module = load_benchmark_module
