from __future__ import annotations

import importlib.util
import sys
from itertools import count
from pathlib import Path
from types import ModuleType

from muster.lab import api

_TASKS = Path(__file__).resolve().parents[1] / "tasks"
_NAMES = count()


class LabError(Exception):
    pass


def _sibling_names(directories: tuple[Path, ...]) -> set[str]:
    names = set()
    for directory in directories:
        for entry in directory.iterdir():
            if entry.suffix == ".py" and entry.stem.isidentifier():
                names.add(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                names.add(entry.name)
    return names


def module(path: Path, name: str, siblings: tuple[Path, ...] = ()) -> ModuleType:
    path = path.resolve()
    directories = tuple(dict.fromkeys((path.parent, *(folder.resolve() for folder in siblings))))
    names = _sibling_names(directories)
    saved = {key: value for key, value in sys.modules.copy().items() if key.split(".")[0] in names}
    saved_path = sys.path[:]
    saved_flag = sys.dont_write_bytecode
    unique = f"_muster_lab_{name}_{next(_NAMES)}"
    spec = importlib.util.spec_from_file_location(unique, path)
    if spec is None or spec.loader is None:
        raise LabError(f"cannot load module from {path}")
    result = importlib.util.module_from_spec(spec)
    for key in saved:
        del sys.modules[key]
    sys.path[:0] = [str(folder) for folder in directories]
    sys.dont_write_bytecode = True
    sys.modules[unique] = result
    try:
        spec.loader.exec_module(result)
        for key, value in sys.modules.copy().items():
            if key in names:
                result.__dict__.setdefault(key, value)
        return result
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        for key in sys.modules.copy():
            if key == unique or key.split(".")[0] in names:
                del sys.modules[key]
        sys.modules.update(saved)


def check_adapter(adapter: object) -> None:
    missing = [name for name in api.ADAPTER_REQUIRED if not hasattr(adapter, name)]
    if missing:
        raise LabError(f"adapter missing required names: {', '.join(missing)}")
    if not isinstance(getattr(adapter, "NAME", None), str):
        raise LabError("adapter NAME must be a string")
    for name in api.ADAPTER_REQUIRED[1:]:
        if not callable(getattr(adapter, name)):
            raise LabError(f"adapter {name} must be callable")


def family(name: str) -> ModuleType:
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise LabError(f"invalid lab family: {name!r}")
    path = _TASKS / name / "lab.py"
    if not path.is_file():
        raise LabError(f"no lab adapter for family {name}: {path}")
    try:
        adapter = module(path, name, siblings=(_TASKS / "_kit",))
    except Exception as error:
        raise LabError(f"cannot load lab family {name}: {error}") from error
    check_adapter(adapter)
    return adapter
