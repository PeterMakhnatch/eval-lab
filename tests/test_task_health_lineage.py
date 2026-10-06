"""Transitive VariantRecord lineage joins for TaskHealthFilter (HAR-172 follow-up).

Every case builds real packages plus real ``derive_task``-generated records and
packages: nothing is hand-echoed. A task-health manifest row binds an original
parent ``P``; ``rewardkit-integrity@1`` descendants (``I1``, ``I2``, ...) must
join that row transitively through retained records, looked up by digest
identity (never by ``Parent.source`` paths). Logical non-resolving chains stay
excluded; they never raise and never report healthy.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_health_filter import SCHEMA, TaskHealthFilter
from evallab.task_variants import VariantRecord, derive_task

SOUND_MIXED = ["health:sound", "solve:mixed"]
REVIEW_MIXED = ["health:review", "solve:mixed"]
INTEGRITY = "rewardkit-integrity@1"
TASK_ID = "format-code-task-000007"


def _package(root: Path, name: str, instruction: str) -> Path:
    pkg = root / name
    (pkg / "tests").mkdir(parents=True)
    (pkg / "task.toml").write_text(
        f'[task]\nname = "{name}"\n[metadata]\ncategory = "Python"\n',
        encoding="utf-8",
    )
    (pkg / "instruction.md").write_text(instruction, encoding="utf-8")
    (pkg / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return pkg


def _row(task_id: str, parent: Path, variant: Path, tags: list[str]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "parent_digest": task_directory_digest(parent),
        "parent_harbor_digest": harbor_task_digest(parent),
        "variant_digest": task_directory_digest(variant),
        "variant_harbor_digest": harbor_task_digest(variant),
        "tags": list(tags),
    }


def _manifest(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"schema": SCHEMA, "tasks": rows}), encoding="utf-8")
    return path


def _trial(job: Path, name: str, *, staged: Path, digest: str) -> Path:
    trial = job / name
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(
        json.dumps({"task": {"path": str(staged)}}), encoding="utf-8"
    )
    (trial / "lock.json").write_text(
        json.dumps(
            {"task": {"name": name, "type": "local", "digest": digest, "path": str(staged)}}
        ),
        encoding="utf-8",
    )
    return trial


def _sidecar(job: Path, **staging: Any) -> None:
    (job / "lab-metadata.json").write_text(
        json.dumps({"task_staging": dict(staging)}), encoding="utf-8"
    )


def _derive(
    parent: Path,
    changes: dict[str, bytes | None],
    transform: str,
    parent_source: dict[str, Any],
    repo: Path,
    variants: Path,
) -> VariantRecord:
    return derive_task(
        parent,
        changes=changes,
        transform=transform,
        rationale="lineage test derivation",
        created_by="pytest",
        parent_source=parent_source,
        repo_root=repo,
        variants_root=variants,
    )


def _scratch(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    records = repo / "library" / "task-variants"
    records.mkdir(parents=True)
    variants = tmp_path / "task-store" / "variants"
    variants.mkdir(parents=True)
    return repo, records, variants


def _chain(tmp_path: Path) -> dict[str, Any]:
    """Build P -> I1 -> I2 with real derive_task records; parents are local-kind."""
    repo, records, variants = _scratch(tmp_path)
    parent = _package(tmp_path / "sources", "pkg-a", "Do the thing.\n")
    r1 = _derive(
        parent,
        {"instruction.md": b"Do the thing, carefully.\n"},
        INTEGRITY,
        {"kind": "local", "path": "canonical/ledger-pkg-a"},
        repo,
        variants,
    )
    v1 = variants / r1.task_slug / r1.digest12
    r2 = _derive(
        v1,
        {"tests/test.sh": b"#!/bin/sh\nexit 0\n# integrity\n"},
        INTEGRITY,
        {"kind": "local", "path": "canonical/variant-parents/v1"},
        repo,
        variants,
    )
    v2 = variants / r2.task_slug / r2.digest12
    assert r1.parent.digest == task_directory_digest(parent)
    assert r2.parent.digest == r1.variant_digest
    assert r2.parent.harbor_digest == r1.variant_harbor_digest
    return {
        "repo": repo,
        "records": records,
        "variants": variants,
        "parent": parent,
        "r1": r1,
        "v1": v1,
        "r2": r2,
        "v2": v2,
    }


def _record_file(repo: Path, record: VariantRecord) -> Path:
    return repo / "library" / "task-variants" / record.task_slug / f"{record.digest12}.json"


def _record_sha(repo: Path, record: VariantRecord) -> str:
    return hashlib.sha256(_record_file(repo, record).read_bytes()).hexdigest()


def _rewrite_record(repo: Path, record: VariantRecord, **updates: Any) -> Path:
    path = _record_file(repo, record)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in updates.items():
        payload[key] = value
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _select(manifest: Path, tags: list[str], mapping: dict[Path, list[Path]], records: Path):
    return TaskHealthFilter(manifest, tags, records_dirs=[records]).select(mapping)


def _stage_copy(chain: dict[str, Any], tmp_path: Path, name: str = "staged-i2") -> Path:
    staged = tmp_path / "runs" / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(chain["v2"], staged)
    return staged


def test_staged_source_multi_hop_lineage_joins_manifest_ancestor(tmp_path: Path):
    chain = _chain(tmp_path)
    # Provenance paths are bogus on purpose: only digest identities may bind.
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(staged))
    _sidecar(
        job,
        source_package_digest=task_directory_digest(chain["v2"]),
        source_harbor_digest=harbor_task_digest(chain["v2"]),
        staged_harbor_digest=harbor_task_digest(staged),
    )
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: [trial]}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "included"
    assert entry["task_id"] == TASK_ID
    binding = entry["binding"]
    assert binding["via"] == "staged-source"
    lineage = binding["lineage"]
    assert [step["variant_digest"] for step in lineage] == [
        chain["r2"].variant_digest,
        chain["r1"].variant_digest,
    ]
    assert [step["parent_digest"] for step in lineage] == [
        chain["r2"].parent.digest,
        chain["r1"].parent.digest,
    ]
    assert [step["transform"] for step in lineage] == [INTEGRITY, INTEGRITY]
    assert lineage[0]["parent_digest"] == lineage[1]["variant_digest"]
    assert lineage[1]["parent_digest"] == task_directory_digest(chain["parent"])
    for step, record in zip(lineage, (chain["r2"], chain["r1"]), strict=True):
        assert step["record"].endswith(f"{record.task_slug}/{record.digest12}.json")
        assert step["record_sha256"] == _record_sha(chain["repo"], record)


def test_native_lock_multi_hop_lineage_joins_manifest_ancestor(tmp_path: Path):
    chain = _chain(tmp_path)
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(chain["v2"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: [trial]}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "included"
    binding = entry["binding"]
    assert binding["via"] == "native-lock"
    lineage = binding["lineage"]
    assert [step["variant_digest"] for step in lineage] == [
        chain["r2"].variant_digest,
        chain["r1"].variant_digest,
    ]
    assert lineage[0]["parent_digest"] == lineage[1]["variant_digest"]


@pytest.mark.parametrize("record_key", ["r1", "r2"])
def test_wrong_parent_harbor_digest_fails_closed(tmp_path: Path, record_key: str):
    chain = _chain(tmp_path)
    record = chain[record_key]
    payload = json.loads(_record_file(chain["repo"], record).read_text(encoding="utf-8"))
    payload["parent"]["harbor_digest"] = "sha256:" + "f" * 64
    _rewrite_record(chain["repo"], record, parent=payload["parent"])
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(chain["v2"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "digest_mismatch"
    assert "task_id" not in entry
    assert report["totals"]["digest_mismatch"] == 1


def test_nearest_manifest_ancestor_with_conflicting_tag_wins(tmp_path: Path):
    chain = _chain(tmp_path)
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(chain["v2"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [
            _row(TASK_ID, chain["v1"], chain["v1"], REVIEW_MIXED),
            _row("format-code-task-000008", chain["parent"], chain["parent"], SOUND_MIXED),
        ],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "tag_mismatch"
    assert entry["task_id"] == TASK_ID


def test_missing_intermediate_record_excludes(tmp_path: Path):
    chain = _chain(tmp_path)
    _record_file(chain["repo"], chain["r1"]).unlink()
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(chain["v2"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: []}
    assert report["trials"][str(trial.absolute())]["status"] == "digest_mismatch"


def test_lineage_cycle_fails_closed(tmp_path: Path):
    chain = _chain(tmp_path)
    payload = json.loads(_record_file(chain["repo"], chain["r1"]).read_text(encoding="utf-8"))
    payload["parent"]["digest"] = chain["r2"].variant_digest
    payload["parent"]["harbor_digest"] = chain["r2"].variant_harbor_digest
    _rewrite_record(chain["repo"], chain["r1"], parent=payload["parent"])
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(chain["v2"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] in {"digest_mismatch", "ambiguous"}
    assert "task_id" not in entry


def test_conflicting_records_for_same_variant_digest_are_ambiguous(tmp_path: Path):
    chain = _chain(tmp_path)
    forged = json.loads(_record_file(chain["repo"], chain["r2"]).read_text(encoding="utf-8"))
    forged["task_name"] = "pkg-b"
    forged["parent"]["digest"] = task_directory_digest(chain["parent"])
    forged["parent"]["harbor_digest"] = harbor_task_digest(chain["parent"])
    conflict = chain["records"] / "pkg-b" / f"{chain['r2'].digest12}.json"
    conflict.parent.mkdir(parents=True)
    conflict.write_text(json.dumps(forged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(chain["v2"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "ambiguous"
    assert "task_id" not in entry


def test_identical_copied_records_still_join(tmp_path: Path):
    chain = _chain(tmp_path)
    duplicate = chain["records"] / "pkg-b" / f"{chain['r2'].digest12}.json"
    duplicate.parent.mkdir(parents=True)
    shutil.copy(_record_file(chain["repo"], chain["r2"]), duplicate)
    staged = _stage_copy(chain, tmp_path)
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=staged, digest=harbor_task_digest(chain["v2"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: [trial]}
    assert report["trials"][str(trial.absolute())]["status"] == "included"


def test_direct_manifest_identity_wins_over_conflicting_record(tmp_path: Path):
    chain = _chain(tmp_path)
    forged = json.loads(_record_file(chain["repo"], chain["r1"]).read_text(encoding="utf-8"))
    forged["task_name"] = "pkg-b"
    forged["parent"]["digest"] = "sha256:" + "1" * 64
    forged["parent"]["harbor_digest"] = "sha256:" + "2" * 64
    conflict = chain["records"] / "pkg-b" / f"{chain['r1'].digest12}.json"
    conflict.parent.mkdir(parents=True)
    conflict.write_text(json.dumps(forged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=chain["v1"], digest=harbor_task_digest(chain["v1"]))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["v1"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: [trial]}
    assert report["trials"][str(trial.absolute())]["status"] == "included"


def test_unrelated_same_name_package_stays_excluded(tmp_path: Path):
    chain = _chain(tmp_path)
    evil = _package(tmp_path / "evil", "pkg-a", "Something entirely different.\n")
    job = tmp_path / "jobs" / "job-a"
    job.mkdir(parents=True)
    trial = _trial(job, "trial-1", staged=evil, digest=harbor_task_digest(evil))
    manifest = _manifest(
        tmp_path / "manifest.json",
        [_row(TASK_ID, chain["parent"], chain["parent"], SOUND_MIXED)],
    )
    selected, report = _select(manifest, ["health:sound"], {job: [trial]}, chain["records"])
    assert selected == {job: []}
    entry = report["trials"][str(trial.absolute())]
    assert entry["status"] == "digest_mismatch"
    assert "task_id" not in entry
