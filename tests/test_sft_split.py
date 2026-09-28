"""Behavioral checks for sealed split-group freezing (evallab.sft_split)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.sft_split import (
    REQUIRED_COLUMNS,
    CatalogTask,
    TraceError,
    build_split,
    group_rank_key,
    heldout_task_ids,
    load_catalog_tasks,
    load_split,
    split_digest,
    write_split,
)
from evallab.sft_split import (
    main as cli_main,
)


def _digest(seed: str) -> str:
    return f"sha256:{hashlib.sha256(seed.encode()).hexdigest()}"


def _row(
    task_id: str,
    *,
    domain: str = "code",
    group: str | None = None,
    name: str | None = None,
    digest: str | None = None,
    repo: str = "FineEnvs/MiMo-V2.6-RL-harbor-code",
    revision: str = "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785",
) -> CatalogTask:
    return CatalogTask(
        domain=domain,
        task_id=task_id,
        task_name=name or f"mimo-v2.6-rl/{task_id}",
        split_group=group or f"{domain}:{task_id}",
        task_version_digest=digest or _digest(f"{domain}/{task_id}"),
        source_repo=repo,
        source_revision=revision,
    )


def _freeze(
    rows: list[CatalogTask],
    *,
    salt: str = "s1",
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    domains = sorted({row.domain for row in rows})
    resolved = counts if counts is not None else {domain: 1 for domain in domains}
    return build_split(
        rows,
        salt=salt,
        catalog_table="/shared/task_versions.parquet",
        catalog_digest=_digest("table"),
        catalog_rows=len(rows),
        heldout_counts=resolved,
    )


def test_split_is_deterministic_and_salt_sensitive() -> None:
    rows = [_row(f"task-{i}", group=f"code:g{i // 2}") for i in range(6)]
    first = _freeze(rows, salt="s1", counts={"code": 2})
    second = _freeze(rows, salt="s1", counts={"code": 2})
    assert split_digest(first) == second["manifest_digest"]
    assert first["manifest_digest"] == second["manifest_digest"]
    other_salt = _freeze(rows, salt="s2", counts={"code": 2})
    assert other_salt["manifest_digest"] != first["manifest_digest"]


def test_group_never_straddles_train_and_heldout() -> None:
    rows = [_row(f"task-{i}", group=f"code:g{i // 2}") for i in range(6)]
    manifest = _freeze(rows, counts={"code": 2})
    by_group: dict[str, set[str]] = {}
    for entry in manifest["tasks"]:
        by_group.setdefault(entry["split_group"], set()).add(entry["split"])
    assert all(splits == {"train"} or splits == {"heldout"} for splits in by_group.values())
    assert manifest["domains"]["code"]["heldout"]["groups"] >= 1


def test_heldout_takes_whole_groups_until_count_met() -> None:
    rows = [
        _row("solo", group="code:solo"),
        *(_row(f"big-{i}", group="code:big") for i in range(3)),
        *(_row(f"mid-{i}", group="code:mid") for i in range(2)),
    ]
    manifest = _freeze(rows, counts={"code": 4})
    block = manifest["domains"]["code"]
    actual = block["heldout"]["actual"]
    assert 4 <= actual <= 4 + 3 - 1  # reached count, overshoot < last group size
    assert actual == len(block["heldout_task_ids"])
    assert len(block["train_task_ids"]) + actual == 6


def test_count_rounding_overshoot_is_reported_not_split() -> None:
    rows = [
        *(_row(f"a-{i}", group="code:a") for i in range(2)),
        *(_row(f"b-{i}", group="code:b") for i in range(2)),
    ]
    manifest = _freeze(rows, counts={"code": 3})
    block = manifest["domains"]["code"]["heldout"]
    assert block["requested"] == 3
    assert block["actual"] == 4  # both pairs whole; never 3 of 4
    assert block["groups"] == 2


def test_requested_zero_holds_out_nothing() -> None:
    rows = [_row(f"task-{i}") for i in range(3)]
    manifest = _freeze(rows, counts={"code": 0})
    assert manifest["counts"] == {"train": 3, "heldout": 0}
    assert manifest["domains"]["code"]["heldout"] == {
        "requested": 0, "actual": 0, "groups": 0,
    }


def test_groups_rank_by_salt_not_task_order() -> None:
    rows = [_row(f"task-{i}") for i in range(4)]
    seen: set[tuple[str, ...]] = set()
    for index in range(20):
        manifest = _freeze(rows, salt=f"salt-{index}", counts={"code": 1})
        seen.add(tuple(manifest["heldout_task_ids"]))
        if len(seen) > 1:
            break
    assert len(seen) > 1  # the salt moves which group ranks first


def test_manifest_shape_and_splits_map() -> None:
    rows = [
        _row("task-a", group="code:family"),
        _row("task-b", group="code:family"),
        _row("task-c", domain="cyber", group="cyber:arvo-x",
             repo="FineEnvs/MiMo-V2.6-RL-harbor-cyber",
             revision="763882ade5fc1a2b3c4d5e6f708192a3b4c5d6e"),
    ]
    manifest = _freeze(rows, counts={"code": 1, "cyber": 0})
    assert manifest["contract"] == "evallab.sft_split/2"
    assert manifest["grouping"] == "split_group"
    assert manifest["catalog"] == {
        "table": "/shared/task_versions.parquet",
        "digest": _digest("table"),
        "rows": 3,
    }
    assert manifest["sources"]["code"].startswith("FineEnvs/MiMo-V2.6-RL-harbor-code@")
    assert set(manifest["splits"]) == {entry["task_version_digest"] for entry in manifest["tasks"]}
    assert set(manifest["splits"].values()) <= {"train", "heldout"}
    assert heldout_task_ids(manifest) == set(manifest["heldout_task_ids"])
    code = manifest["domains"]["code"]
    assert code["train_task_ids"] == sorted(code["train_task_ids"])
    assert sorted(code["train_task_ids"] + code["heldout_task_ids"]) == [
        "task-a", "task-b",
    ]
    family = {entry["split"] for entry in manifest["tasks"] if entry["split_group"] == "code:family"}
    assert family == {"heldout"}  # first-ranked group, whole, at count 1


def test_export_eligible_round_trip_reads_splits_map(tmp_path: Path) -> None:
    from evallab.task_catalog import _read_split_map

    rows = [_row(f"task-{i}") for i in range(2)]
    manifest = _freeze(rows, counts={"code": 1})
    path = tmp_path / "split.json"
    write_split(manifest, path)
    mapping = _read_split_map(path)
    assert mapping == manifest["splits"]
    heldout = next(
        entry["task_version_digest"]
        for entry in manifest["tasks"]
        if entry["split"] == "heldout"
    )
    assert mapping[heldout] == "heldout"


def test_missing_domain_count_refuses() -> None:
    with pytest.raises(TraceError, match="no held-out count"):
        _freeze([_row("task-a")], counts={})


def test_count_out_of_range_refuses() -> None:
    rows = [_row("task-a")]
    with pytest.raises(TraceError, match="outside 0..1"):
        _freeze(rows, counts={"code": 2})


def test_malformed_digest_refuses(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {name: ["code", "task-a", "mimo-v2.6-rl/task-a", "code:g", "not-a-digest",
                "FineEnvs/MiMo-V2.6-RL-harbor-code", "a" * 40]
         for name in REQUIRED_COLUMNS}
    )
    path = tmp_path / "bad-digest.parquet"
    pq.write_table(table, path)
    with pytest.raises(TraceError, match="malformed"):
        load_catalog_tasks(path)


def test_missing_catalog_columns_refuse(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"domain": ["code"], "task_id": ["task-a"]})
    path = tmp_path / "thin.parquet"
    pq.write_table(table, path)
    with pytest.raises(TraceError, match="lacks required columns"):
        load_catalog_tasks(path)


def test_multi_source_domain_refuses() -> None:
    rows = [
        _row("task-a", revision="a" * 40),
        _row("task-b", revision="b" * 40),
    ]
    with pytest.raises(TraceError, match="multiple pinned sources"):
        _freeze(rows, counts={"code": 1})


def test_sealed_overwrite_and_tamper(tmp_path: Path) -> None:
    manifest = _freeze([_row("task-a"), _row("task-b")], counts={"code": 1})
    path = tmp_path / "split.json"
    write_split(manifest, path)
    assert heldout_task_ids(load_split(path)) == heldout_task_ids(manifest)
    with pytest.raises(TraceError, match="already exists"):
        write_split(manifest, path)
    edited = json.loads(path.read_text())
    edited["tasks"][0]["split"] = (
        "train" if edited["tasks"][0]["split"] == "heldout" else "heldout"
    )
    path.write_text(json.dumps(edited))
    with pytest.raises(TraceError, match="digest mismatch"):
        load_split(path)


def test_cli_freeze_end_to_end(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [_row(f"task-{i}", group=f"code:g{i // 2}") for i in range(4)]
    table = pa.table(
        {name: [getattr(row, name) for row in rows] for name in REQUIRED_COLUMNS}
    )
    catalog = tmp_path / "task_versions.parquet"
    pq.write_table(table, catalog)
    out = tmp_path / "split.json"
    assert (
        cli_main(
            [
                "freeze", "--salt", "cli-salt", "--heldout-count", "code=1",
                "--catalog", catalog.as_posix(), "--out", out.as_posix(),
            ]
        )
        == 0
    )
    manifest = load_split(out)
    assert manifest["catalog"]["rows"] == 4
    assert manifest["domains"]["code"]["heldout"]["requested"] == 1
    by_group = {
        entry["split_group"]: entry["split"] for entry in manifest["tasks"]
    }
    assert set(by_group.values()) == {"train", "heldout"}


def test_group_rank_key_is_stable() -> None:
    assert group_rank_key("s", "code:g") == group_rank_key("s", "code:g")
    assert group_rank_key("s", "code:g") != group_rank_key("s", "code:h")
