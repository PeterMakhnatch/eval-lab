"""Behavioral checks for sealed train/held-out split freezing (evallab.sft_split)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.sft_split import (
    DomainRoot,
    TraceError,
    build_split,
    heldout_task_ids,
    load_split,
    rank_key,
    split_digest,
    write_split,
)
from evallab.sft_split import (
    main as cli_main,
)


def _task(root: Path, task_id: str, *, body: str = "print('x')\n") -> Path:
    package = root / task_id
    package.mkdir(parents=True)
    (package / "task.toml").write_text(f"[task]\nname = '{task_id}'\n")
    (package / "solution.py").write_text(body)
    return package


def _roots(tmp_path: Path, *domains: str) -> list[DomainRoot]:
    out = []
    for domain in domains:
        root = tmp_path / domain
        root.mkdir()
        out.append(DomainRoot(domain=domain, path=root))
    return out


def test_split_is_deterministic_and_salt_sensitive(tmp_path: Path) -> None:
    code, = _roots(tmp_path, "code")
    for i in range(6):
        _task(code.path, f"task-{i}")
    first = build_split(
        [code], salt="s1", source_dataset="FineEnvs/MiMo-V2.6-RL-harbor",
        source_revision="abc123", heldout_counts={"code": 2}, heldout_fraction=None,
    )
    second = build_split(
        [code], salt="s1", source_dataset="FineEnvs/MiMo-V2.6-RL-harbor",
        source_revision="abc123", heldout_counts={"code": 2}, heldout_fraction=None,
    )
    assert split_digest(first) == second["manifest_digest"]
    assert first["manifest_digest"] == second["manifest_digest"]
    assert first["counts"] == {"train": 4, "heldout": 2}
    other_salt = build_split(
        [code], salt="s2", source_dataset="FineEnvs/MiMo-V2.6-RL-harbor",
        source_revision="abc123", heldout_counts={"code": 2}, heldout_fraction=None,
    )
    assert other_salt["manifest_digest"] != first["manifest_digest"]


def test_heldout_are_the_lowest_ranked_task_ids(tmp_path: Path) -> None:
    code, = _roots(tmp_path, "code")
    ids = [f"task-{i}" for i in range(5)]
    for task_id in ids:
        _task(code.path, task_id)
    manifest = build_split(
        [code], salt="salt", source_dataset="ds", source_revision="r",
        heldout_counts={"code": 2}, heldout_fraction=None,
    )
    ranked = sorted(ids, key=lambda task_id: (rank_key("salt", task_id), task_id))
    assert heldout_task_ids(manifest) == set(ranked[:2])
    tasks = {task["task_id"]: task for task in manifest["domains"]["code"]["tasks"]}
    assert tasks[ranked[0]]["assignment"] == "heldout"
    assert tasks[ranked[2]]["assignment"] == "train"
    assert all(task["package_digest"].startswith("sha256:") for task in tasks.values())


def test_fraction_applies_per_domain_and_count_overrides(tmp_path: Path) -> None:
    code, cyber = _roots(tmp_path, "code", "cyber")
    for i in range(10):
        _task(code.path, f"c-{i}")
        _task(cyber.path, f"y-{i}")
    manifest = build_split(
        [code, cyber], salt="s", source_dataset="ds", source_revision="r",
        heldout_counts={"cyber": 5}, heldout_fraction=0.2,
    )
    assert manifest["domains"]["code"]["heldout_count"] == 2
    assert manifest["domains"]["cyber"]["heldout_count"] == 5
    assert manifest["counts"] == {"train": 13, "heldout": 7}


def test_domain_without_explicit_heldout_size_refuses(tmp_path: Path) -> None:
    code, = _roots(tmp_path, "code")
    _task(code.path, "only")
    with pytest.raises(TraceError, match="no explicit held-out count"):
        build_split(
            [code], salt="s", source_dataset="ds", source_revision="r",
            heldout_counts={}, heldout_fraction=None,
        )


def test_duplicate_task_id_across_domains_refuses(tmp_path: Path) -> None:
    code, cyber = _roots(tmp_path, "code", "cyber")
    _task(code.path, "shared")
    _task(cyber.path, "shared")
    with pytest.raises(TraceError, match="globally unique"):
        build_split(
            [code, cyber], salt="s", source_dataset="ds", source_revision="r",
            heldout_counts={"code": 0, "cyber": 0}, heldout_fraction=None,
        )


def test_edited_manifest_fails_digest_verification(tmp_path: Path) -> None:
    code, = _roots(tmp_path, "code")
    _task(code.path, "a")
    _task(code.path, "b")
    manifest = build_split(
        [code], salt="s", source_dataset="ds", source_revision="r",
        heldout_counts={"code": 1}, heldout_fraction=None,
    )
    path = tmp_path / "split.json"
    write_split(manifest, path)
    assert heldout_task_ids(load_split(path)) == heldout_task_ids(manifest)
    edited = json.loads(path.read_text())
    first = edited["domains"]["code"]["tasks"][0]
    first["assignment"] = "train" if first["assignment"] == "heldout" else "heldout"
    path.write_text(json.dumps(edited))
    with pytest.raises(TraceError, match="digest mismatch"):
        load_split(path)


def test_write_refuses_to_replace_sealed_manifest(tmp_path: Path) -> None:
    code, = _roots(tmp_path, "code")
    _task(code.path, "a")
    manifest = build_split(
        [code], salt="s", source_dataset="ds", source_revision="r",
        heldout_counts={"code": 0}, heldout_fraction=None,
    )
    path = tmp_path / "split.json"
    write_split(manifest, path)
    with pytest.raises(TraceError, match="sealed"):
        write_split(manifest, path)
    write_split(manifest, path, force=True)


def test_cli_freeze_end_to_end(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    for i in range(4):
        _task(code, f"t{i}")
    out = tmp_path / "out" / "split.json"
    exit_code = cli_main(
        [
            "freeze",
            "--root", f"code={code}",
            "--salt", "abc",
            "--source-dataset", "FineEnvs/MiMo-V2.6-RL-harbor-code",
            "--source-revision", "deadbeef",
            "--heldout-count", "code=1",
            "--out", str(out),
        ]
    )
    assert exit_code == 0
    manifest = load_split(out)
    assert manifest["source_dataset"] == "FineEnvs/MiMo-V2.6-RL-harbor-code"
    assert manifest["source_revision"] == "deadbeef"
    assert manifest["counts"]["heldout"] == 1
    # Re-running onto the sealed file refuses.
    assert cli_main(
        [
            "freeze",
            "--root", f"code={code}",
            "--salt", "abc",
            "--source-dataset", "FineEnvs/MiMo-V2.6-RL-harbor-code",
            "--source-revision", "deadbeef",
            "--heldout-count", "code=1",
            "--out", str(out),
        ]
    ) == 2
