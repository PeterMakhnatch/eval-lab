from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evallab.failure_library.sources.swe_family import (
    SplitPolicy,
    pinned_bytes,
)


@pytest.fixture
def policy(tmp_path: Path) -> SplitPolicy:
    assignments = tmp_path / "research/answer-keys/splits/assignments.csv"
    assignments.parent.mkdir(parents=True)
    assignments.write_text(
        "key_id,item_id,task,unit,split,expected_verdict,exposures\n"
        "older,i1,astropy__astropy-12907,u1,dev,fail,\n"
        "reported,i2,swebench/astropy__astropy-12907@pin,u2,test,fail,\n"
        "cheatbench-actions-v1,i3,cross-task,u3,dev,fail,\n",
        encoding="utf-8",
    )
    return SplitPolicy(tmp_path)


def test_split_policy_blocks_cross_key_reported_tasks_and_groups_variants(
    policy: SplitPolicy,
) -> None:
    assert policy.get("astropy__astropy-12907", "swe-bench/astropy__astropy-12907") == "TEST"
    assert policy.get("cross-task", "new/cross-task") == "cross_test"
    assert policy.get("new-task/oneoff", "shared-family") == policy.get(
        "new-task/conflicting", "shared-family"
    )
    group = "shared-family"
    expected = "heldout" if int(hashlib.sha256(group.encode()).hexdigest(), 16) % 5 == 0 else "DEV"
    assert policy.get("new-task", group) == expected


def test_raw_snapshot_refuses_modified_source_and_survives_missing_origin(tmp_path: Path) -> None:
    origin = tmp_path / "origin.json"
    raw = b'{"source":"fixture"}'
    origin.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    state = tmp_path / "state"
    assert (
        pinned_bytes("fixture", "revision", "data.json", digest, state_root=state, local=origin)
        == raw
    )
    cached = state / "raw/fixture/revision/data.json"
    assert cached.stat().st_mode & 0o222 == 0
    origin.unlink()
    assert pinned_bytes("fixture", "revision", "data.json", digest, state_root=state) == raw
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        pinned_bytes("fixture", "revision", "data.json", "0" * 64, state_root=state)
