"""Extend the frozen transport lifetime to cover its existing run and grade budgets.

The immutable underlying transport retains every tool and container boundary.
The watchdog still removes the owned container when its parent pipe closes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import selectors
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "ov1104-impossible/container_tool.py"
SOURCE_SHA256 = "f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a"
LIFETIME_SECONDS = 3000  # 900 agent + 2 * 900 grade + 300 setup/capture margin.
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SOURCE_SHA256:
    raise RuntimeError("Underlying frozen transport differs")
_spec = importlib.util.spec_from_file_location("impmore_frozen_transport", SOURCE)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
_base.__file__ = __file__
_create = _base.create_container
_canary = _base.canary


def create_container(*args, **kwargs):
    kwargs["sleep_s"] = LIFETIME_SECONDS
    return _create(*args, **kwargs)


def canary(*args, **kwargs):
    # The explicit-cwd canary driver supplies its isolated launch proxy.
    _base.subprocess = globals().get("subprocess", _base.subprocess)
    return _canary(*args, **kwargs)


def watchdog_main(config_path):
    config = _base.json.loads(config_path.read_text())
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    _base.save_new(config_path.parent / "watchdog-ready.json", {"pid": _base.os.getpid(),
                   "lifetime_seconds": LIFETIME_SECONDS, "source_sha256": SOURCE_SHA256})
    try:
        selector.select(LIFETIME_SECONDS)
        result = _base.docker(config, "inspect", config["container_id"])
        if result.returncode == 0:
            _base.cleanup(config)
    finally:
        selector.close()


_base.create_container = create_container
_base.watchdog_main = watchdog_main
for _name in dir(_base):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_base, _name))

if __name__ == "__main__":
    _base.main()
