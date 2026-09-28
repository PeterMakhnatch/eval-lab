"""Focused behavior tests for task variants (``evallab.task_variant/v1``).

Everything happens in injected scratch directories: a scratch repo root for
the git-tracked records and a scratch variants store, so no test touches the
shared derived store or the real checkout.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_variants import (
    LineageError,
    VariantError,
    VariantExistsError,
    VariantInvalid,
    VariantRecord,
    append_status_evidence,
    default_variants_root,
    derive_task,
    lineage_chain,
    load_records,
    load_variant_records,
    materialize,
    render_lineage,
    verify,
)

TASK_TOML = 'schema_version = "1.4"\n\n[task]\nname = "mimo-v2.6-rl/pkg-a"\ndescription = "demo"\n'


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _mini_task(root: Path, *, name: str = "pkg-a", instruction: str = "Do the thing.\n") -> Path:
    pkg = root / name
    (pkg / "environment").mkdir(parents=True)
    (pkg / "tests").mkdir()
    (pkg / "task.toml").write_text(
        TASK_TOML if name == "pkg-a" else TASK_TOML.replace("pkg-a", name),
        encoding="utf-8",
    )
    (pkg / "instruction.md").write_text(instruction, encoding="utf-8")
    (pkg / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    (pkg / "tests" / "test.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    return pkg


def _scratch(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Scratch (repo_root, records_dir, variants_root); repo_root is a git-free tree."""
    repo = tmp_path / "repo"
    records = repo / "library" / "task-variants"
    records.mkdir(parents=True)
    variants = tmp_path / "task-store" / "variants"
    variants.mkdir(parents=True)
    sources = tmp_path / "sources"
    sources.mkdir()
    return repo, records, variants


def _derive(
    parent: Path,
    *,
    changes: dict[str, bytes | None],
    repo: Path,
    variants: Path,
    transform: str = "manual-fix@1",
    **kwargs,
) -> VariantRecord:
    return derive_task(
        parent,
        changes=changes,
        transform=transform,
        rationale="test derivation",
        created_by="pytest",
        repo_root=repo,
        records_dir=Path("library/task-variants"),
        variants_root=variants,
        **kwargs,
    )


def _record_path(records: Path, record: VariantRecord) -> Path:
    return records / record.task_slug / f"{record.digest12}.json"


def test_derive_writes_record_and_package_with_pinned_digests(tmp_path: Path) -> None:
    repo, records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    record = _derive(
        parent,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo,
        variants=variants,
    )
    assert record.task_name == "mimo-v2.6-rl/pkg-a"
    assert record.task_slug == "mimo-v2.6-rl__pkg-a"
    assert record.transform == "manual-fix@1"
    assert record.components_changed == ["instruction"]
    package_dir = variants / record.task_slug / record.digest12
    assert package_dir.is_dir()
    assert task_directory_digest(package_dir) == record.variant_digest
    assert harbor_task_digest(package_dir) == record.variant_harbor_digest
    assert record.parent.digest == task_directory_digest(parent)
    assert record.parent.harbor_digest == harbor_task_digest(parent)
    record_path = _record_path(records, record)
    assert record_path.is_file()
    assert VariantRecord.model_validate(json.loads(record_path.read_text("utf-8"))) == record
    (change,) = record.files
    assert change.path == "instruction.md"
    parent_bytes = (parent / "instruction.md").read_bytes()
    variant_bytes = (package_dir / "instruction.md").read_bytes()
    assert change.before_sha256 == _sha(parent_bytes)
    assert change.after_sha256 == _sha(variant_bytes)
    assert change.content == variant_bytes.decode("utf-8")
    assert (package_dir / "tests" / "test.sh").read_bytes() == (
        parent / "tests" / "test.sh"
    ).read_bytes()
    assert verify(record, repo_root=repo, variants_root=variants) == []


def test_derive_is_location_independent_and_refuses_overwrite(tmp_path: Path) -> None:
    repo, _records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    record = _derive(
        parent,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo,
        variants=variants,
    )
    # Same bytes derived from a relocated parent copy land on the same digest.
    relocated = tmp_path / "elsewhere" / "pkg-a"
    relocated.parent.mkdir(parents=True)
    shutil.copytree(parent, relocated)
    repo2 = tmp_path / "repo2"
    (repo2 / "library" / "task-variants").mkdir(parents=True)
    variants2 = tmp_path / "task-store2" / "variants"
    variants2.mkdir(parents=True)
    twin = _derive(
        relocated,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo2,
        variants=variants2,
    )
    assert twin.variant_digest == record.variant_digest
    # Re-deriving the identical change refuses to overwrite either artifact.
    with pytest.raises(VariantExistsError, match="already exists"):
        _derive(
            parent,
            changes={"instruction.md": b"Do the thing, carefully.\n"},
            repo=repo,
            variants=variants,
        )


def test_materialize_rebuilds_deleted_package_with_identical_digest(tmp_path: Path) -> None:
    repo, records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    record = _derive(
        parent,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo,
        variants=variants,
    )
    package_dir = variants / record.task_slug / record.digest12
    shutil.rmtree(package_dir)
    assert not package_dir.exists()
    rebuilt = materialize(record, parent, repo_root=repo, variants_root=variants)
    assert rebuilt == package_dir
    assert task_directory_digest(rebuilt) == record.variant_digest
    # Idempotent: an existing, matching package is returned untouched.
    again = materialize(record, parent, repo_root=repo, variants_root=variants)
    assert again == package_dir
    # A tampered record cannot rebuild its package: the inline digest proof fails.
    shutil.rmtree(package_dir)
    payload = json.loads(_record_path(records, record).read_text("utf-8"))
    payload["files"][0]["content"] = "forged instruction bytes\n"
    forged = VariantRecord.model_validate(payload)
    with pytest.raises(VariantError, match="inline content digest mismatch"):
        materialize(forged, parent, repo_root=repo, variants_root=variants)
    assert not package_dir.exists()


def test_verify_reports_tampering_and_drifted_parent(tmp_path: Path) -> None:
    repo, _records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    record = _derive(
        parent,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo,
        variants=variants,
    )
    package_dir = variants / record.task_slug / record.digest12
    (package_dir / "tests" / "test.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    failures = verify(record, repo_root=repo, variants_root=variants)
    assert any("variant digest mismatch" in failure for failure in failures)
    drifted = tmp_path / "drifted-parent"
    shutil.copytree(parent, drifted)
    (drifted / "instruction.md").write_text("different original\n", encoding="utf-8")
    failures = verify(record, parent_dir=drifted, repo_root=repo, variants_root=variants)
    assert any("parent digest mismatch" in failure for failure in failures)
    assert any("parent file digest mismatch" in failure for failure in failures)


def test_second_generation_chain_missing_parent_and_cycle(tmp_path: Path) -> None:
    repo, records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    first = _derive(
        parent,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo,
        variants=variants,
        transform="instruction-candidate@1",
    )
    first_record_relpath = f"library/task-variants/{first.task_slug}/{first.digest12}.json"
    first_package = variants / first.task_slug / first.digest12
    second = _derive(
        first_package,
        changes={"tests/test.sh": b"#!/bin/bash\nset -eu\nexit 0\n"},
        repo=repo,
        variants=variants,
        transform="manual-fix@1",
        parent_source={"kind": "variant", "record": first_record_relpath},
    )
    assert second.parent.digest == first.variant_digest
    assert second.parent.source.kind == "variant"
    assert second.components_changed == ["verifier"]
    steps = lineage_chain(second, repo_root=repo)
    assert [step.record.variant_digest for step in steps] == [
        second.variant_digest,
        first.variant_digest,
    ]
    rendered = render_lineage(steps)
    assert "instruction-candidate@1" in rendered
    assert "manual-fix@1" in rendered
    assert "components_changed: verifier" in rendered
    assert "chain terminates at the original" in rendered
    # Deleting the parent's record leaves the chain walkable with an error step.
    _record_path(records, first).unlink()
    steps = lineage_chain(second, repo_root=repo)
    assert steps[-1].parent_error is not None
    assert "missing parent lineage record" in steps[-1].parent_error
    assert any("ERROR" in line for line in render_lineage(steps).splitlines())
    # A self-referential record is reported as a cycle, not walked forever.
    cyclic_relpath = f"library/task-variants/{second.task_slug}/cyclic.json"
    payload = second.model_dump(mode="json", by_alias=True)
    payload["parent"] = {
        "digest": second.variant_digest,
        "harbor_digest": second.variant_harbor_digest,
        "source": {"kind": "variant", "record": cyclic_relpath},
    }
    cyclic_path = repo / cyclic_relpath
    cyclic_path.parent.mkdir(parents=True, exist_ok=True)
    cyclic_path.write_text(json.dumps(payload), encoding="utf-8")
    steps = lineage_chain(cyclic_path, repo_root=repo)
    assert steps[-1].parent_error is not None
    assert "cycle" in steps[-1].parent_error


def test_refusals_binary_oversize_symlink_escape_and_noop(tmp_path: Path) -> None:
    repo, _records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    common = {"repo": repo, "variants": variants}
    with pytest.raises(VariantInvalid, match="not valid UTF-8"):
        _derive(parent, changes={"environment/setup.sh": b"\x00\xff\xfe"}, **common)
    with pytest.raises(VariantInvalid, match="inline limit"):
        _derive(parent, changes={"instruction.md": b"a" * (1024 * 1024 + 1)}, **common)
    with pytest.raises(VariantInvalid, match="escapes"):
        _derive(parent, changes={"../outside.txt": b"text\n"}, **common)
    with pytest.raises(VariantInvalid, match="cannot delete"):
        _derive(parent, changes={"absent.txt": None}, **common)
    with pytest.raises(VariantInvalid, match="no changes requested"):
        _derive(parent, changes={}, **common)
    with pytest.raises(VariantInvalid, match="no-op change"):
        _derive(
            parent,
            changes={"instruction.md": (parent / "instruction.md").read_bytes()},
            **common,
        )
    link_host = tmp_path / "linked"
    shutil.copytree(parent, link_host)
    (link_host / "environment" / "link").symlink_to("../task.toml")
    with pytest.raises(VariantInvalid, match="symlink"):
        _derive(link_host, changes={"instruction.md": b"changed\n"}, **common)
    with pytest.raises(VariantInvalid, match="name@version"):
        _derive(parent, changes={"instruction.md": b"changed\n"}, transform="manual", **common)


def test_deletion_variant_and_component_classification(tmp_path: Path) -> None:
    repo, _records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    (parent / "notes.md").write_text("scratch notes\n", encoding="utf-8")
    record = _derive(
        parent,
        changes={"notes.md": None, "environment/Dockerfile": b"FROM debian:12\n"},
        repo=repo,
        variants=variants,
    )
    assert set(record.components_changed) == {"environment"}
    (change,) = [c for c in record.files if c.path == "notes.md"]
    assert change.after_sha256 is None and change.content is None
    package_dir = variants / record.task_slug / record.digest12
    assert not (package_dir / "notes.md").exists()
    assert (package_dir / "environment" / "Dockerfile").read_bytes() == b"FROM debian:12\n"
    assert verify(record, repo_root=repo, variants_root=variants) == []


def test_status_evidence_is_append_only_and_one_way(tmp_path: Path) -> None:
    repo, _records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    record = _derive(
        parent,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo,
        variants=variants,
    )
    updated = append_status_evidence(
        record,
        "validated",
        evidence="runs/oracle-20260928 (oracle pass, nop fail)",
        by="peter",
        repo_root=repo,
    )
    assert updated.status == "validated"
    assert len(updated.evidence) == 1
    assert updated.evidence[0].evidence == "runs/oracle-20260928 (oracle pass, nop fail)"
    assert updated.model_dump(mode="json", by_alias=True, exclude={"status", "evidence"}) == (
        record.model_dump(mode="json", by_alias=True, exclude={"status", "evidence"})
    )
    reloaded = load_records(repo)[0]
    assert reloaded.status == "validated"
    with pytest.raises(VariantError, match="final"):
        append_status_evidence(
            updated, "rejected", evidence="later verdict", by="peter", repo_root=repo
        )
    again = append_status_evidence(
        updated, "validated", evidence="second confirming job", by="agent", repo_root=repo
    )
    assert len(again.evidence) == 2
    assert again.status == "validated"


def test_load_records_fail_closed_and_plain_dict_loader(tmp_path: Path) -> None:
    repo, records, variants = _scratch(tmp_path)
    parent = _mini_task(tmp_path / "sources")
    record = _derive(
        parent,
        changes={"instruction.md": b"Do the thing, carefully.\n"},
        repo=repo,
        variants=variants,
    )
    dicts = load_variant_records(repo)
    assert len(dicts) == 1
    assert dicts[0]["schema"] == "evallab.task_variant/v1"
    assert dicts[0]["variant_digest"] == record.variant_digest
    broken = records / record.task_slug / "broken00000000.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(LineageError, match="unreadable"):
        load_records(repo)
    assert load_records(tmp_path / "empty-repo") == []


def test_default_variants_root_is_shared_checkout_derived(tmp_path: Path) -> None:
    # A git-free directory resolves to itself, so the store lands beside it.
    root = tmp_path / "plain"
    root.mkdir()
    assert default_variants_root(root) == root / "derived" / "task-store" / "variants"
