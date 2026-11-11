"""The native bank writer lock survives retirement of the refresh producer."""

from __future__ import annotations

import threading
from pathlib import Path

from evallab.failure_library.bank import IssueBank, bank_writer_lock


def test_writer_lock_is_reentrant_and_shared_by_manifest_across_state_roots(tmp_path: Path) -> None:
    first = IssueBank(tmp_path / "repo", state_root=tmp_path / "state")
    second = IssueBank(tmp_path / "repo", state_root=tmp_path / "other-state")
    attempting, entered = threading.Event(), threading.Event()

    def contender() -> None:
        attempting.set()
        with bank_writer_lock(second):
            entered.set()

    thread = threading.Thread(target=contender, daemon=True)
    with bank_writer_lock(first), bank_writer_lock(first):
        thread.start()
        assert attempting.wait(2)
        assert not entered.wait(0.05)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert entered.is_set()
