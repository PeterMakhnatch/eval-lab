"""``ChangeCache``: one load per file state, and a thrown load never poisons the cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.production.file_cache import ChangeCache, signature


def test_change_cache_loads_once_per_state_and_a_thrown_load_never_poisons_it(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    attempts: list[str] = []
    fail = True

    def flaky(candidate: Path) -> dict[str, object]:
        attempts.append(candidate.name)
        if fail:
            raise MemoryError("out of memory while reading")
        return {"text": candidate.read_text(encoding="utf-8")}

    cache = ChangeCache(path, flaky)
    assert signature(path) is None  # an absent file is one state, not an error
    with pytest.raises(MemoryError):
        cache.get()
    fail = False
    path.write_text("first", encoding="utf-8")
    assert cache.get() == {"text": "first"} and len(attempts) == 2  # the failed read is retried
    assert cache.get() == {"text": "first"} and len(attempts) == 2  # unchanged: no second read

    fail = True
    path.write_text("second", encoding="utf-8")
    with pytest.raises(MemoryError):
        cache.get()  # a changed state is read, never skipped as already loaded
    with pytest.raises(MemoryError):
        cache.get()  # and the failure is not remembered as a loaded state
    fail = False
    assert cache.get() == {"text": "second"} and len(attempts) == 5
    assert cache.get() == {"text": "second"} and len(attempts) == 5
