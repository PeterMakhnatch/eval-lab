"""Keep synthetic line tests independent of unrelated host ancestor contents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from evallab import rollout_admission


@pytest.fixture(autouse=True)
def isolated_admission_ancestors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every listing inside the test real, but supply clean host ancestors.

    Admission checks every parent up to the filesystem root. Host temporary
    directories contain unrelated tests and are not inputs to these cases.
    Preserve the canonical path component at each host parent. All test-owned
    sidecars, links, and directory entries still pass through real admission.
    """
    root = tmp_path.resolve()
    ancestors = {str(parent): root.relative_to(parent).parts[0] for parent in root.parents}

    class AdmissionOS:
        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

        def listdir(self, path: Any = ".") -> list[Any]:
            if not isinstance(path, int):
                child = ancestors.get(os.fspath(path))
                if child is not None:
                    return [child]
            return os.listdir(path)

    monkeypatch.setattr(rollout_admission, "os", AdmissionOS())
