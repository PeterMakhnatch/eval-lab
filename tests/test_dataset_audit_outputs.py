"""Audit tag publication and dossier export: real bytes, real manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.dataset_audit_contracts import AuditRecord, AuditTask
from evallab.dataset_audit_outputs import export_audit_dossiers, publish_audit_tags
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_dossier import dossier_for_audit
from evallab.task_health_filter import TaskHealthFilter
from evallab.task_health_tags import SCHEMA

DIGEST_A = "sha256:" + "a" * 64


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


def _record(
    pkg: Path | None,
    *,
    task_id: str = "hello-world",
    verdict: str = "keep",
    source_uri: str = "file://hello",
) -> AuditRecord:
    task = AuditTask(
        dataset_id="hello",
        task_id=task_id,
        task_name="harbor/hello-world",
        path=pkg,
        package_digest=task_directory_digest(pkg) if pkg is not None else None,
        harbor_digest=harbor_task_digest(pkg) if pkg is not None else None,
        aliases=("harbor/hello-world",),
        source_uri=source_uri,
        revision="1.0",
    )
    return AuditRecord(task=task, verdict=verdict, verdict_reason="stored routing")  # type: ignore[arg-type]


def _trial(job: Path, name: str, digest: str) -> Path:
    trial = job / name
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(json.dumps({"task": {"path": "/staged/pkg"}}))
    (trial / "lock.json").write_text(
        json.dumps({"task": {"name": name, "type": "local", "digest": digest, "path": "/staged/pkg"}})
    )
    return trial


def test_publish_binds_real_package_and_source_bytes_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pkg = _package(root / "sources", "harbor/hello-world", "Say hello.\n")
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(pkg.rglob("*")) if path.is_file()}
    record = _record(pkg)

    manifest = publish_audit_tags([record], repo_root=root, output_dir=tmp_path / "out")

    assert manifest.name == "task-health.json"
    payload = json.loads(manifest.read_text())
    assert payload["schema"] == SCHEMA
    assert payload["certification"] is False
    (row,) = payload["tasks"]
    assert row["tags"] == ["verdict:keep"]
    assert row["parent_digest"] == task_directory_digest(pkg)
    assert row["parent_harbor_digest"] == harbor_task_digest(pkg)
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(pkg.rglob("*")) if path.is_file()}
    assert after == before  # source packages are never edited
    selected, _ = TaskHealthFilter(manifest, ["verdict:keep"]).select(
        {tmp_path / "job": [_trial(tmp_path / "job", "t__1", harbor_task_digest(pkg))]}
    )
    assert selected == {tmp_path / "job": [tmp_path / "job" / "t__1"]}


def test_unknown_verdict_is_published_not_keep(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pkg = _package(root / "sources", "harbor/hello-world", "Say hello.\n")
    manifest = publish_audit_tags(
        [_record(pkg, verdict="unknown")], repo_root=root, output_dir=tmp_path / "out"
    )
    payload = json.loads(manifest.read_text())
    (row,) = payload["tasks"]
    assert "verdict:unknown" in row["tags"]
    assert "verdict:keep" not in row["tags"]
    assert payload["coverage"]["unknown"] == 1
    job = tmp_path / "job"
    trial = _trial(job, "t__1", harbor_task_digest(pkg))
    selected, _ = TaskHealthFilter(manifest, ["verdict:keep"]).select({job: [trial]})
    assert selected == {job: []}  # explicit unknown coverage never passes a keep filter
    selected, _ = TaskHealthFilter(manifest, ["verdict:unknown"]).select({job: [trial]})
    assert selected == {job: [trial]}


def test_unbound_record_never_becomes_keep(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no audit tag variant could be bound"):
        publish_audit_tags(
            [_record(None, task_id="missing-pkg")],
            repo_root=tmp_path,
            output_dir=tmp_path / "out",
        )
    assert not (tmp_path / "out" / "task-health.json").exists()


def test_digest_disagreement_is_unbound_not_retagged(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pkg = _package(root / "sources", "harbor/hello-world", "Say hello.\n")
    record = _record(pkg)
    tampered = record.model_copy(
        update={"task": record.task.model_copy(update={"package_digest": DIGEST_A})}
    )
    with pytest.raises(ValueError, match="no audit tag variant could be bound"):
        publish_audit_tags([tampered], repo_root=root, output_dir=tmp_path / "out")


def test_export_uses_collision_safe_names_and_explicit_history(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first = _package(root / "sources-a", "harbor/hello-world", "Say hello.\n")
    second = _package(root / "sources-b", "harbor/hello-world", "Say hello loudly.\n")
    records = [_record(first, source_uri="file://a"), _record(second, source_uri="file://b")]

    index = export_audit_dossiers(
        records, repo_root=root, output_dir=tmp_path / "out",
        roots=[], derived_root=tmp_path / "derived",
        reader_store=tmp_path / "readers", trial_facets={},
    )

    payload = json.loads(index.read_text())
    assert payload["schema"] == "evallab.audit_dossiers/v1"
    paths = [entry["path"] for entry in payload["dossiers"]]
    assert len(set(paths)) == 2  # package versions never share a filename
    for entry, record in zip(payload["dossiers"], records, strict=True):
        assert entry["history"] == "not_collected"
        dossier = json.loads(Path(entry["path"]).read_text())
        assert dossier["audit"]["status"] == "recorded"
        assert dossier["audit"]["verdict"] == "keep"
        assert dossier["audit"]["package_digest"] == record.task.package_digest
        assert dossier["trials"] is None
        assert dossier["coverage"]["collected"] is False


def test_export_supplied_facets_skip_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evallab.task_dossier_trials as trials_module

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("census must not run when facets are supplied")

    monkeypatch.setattr(trials_module, "task_trials_for_tasks", _forbidden)
    root = tmp_path / "repo"
    pkg = _package(root / "sources", "harbor/hello-world", "Say hello.\n")
    record = _record(pkg)
    facet = {"trials": [{"job": "j", "trial": "t", "task_digest": None}], "coverage": {"task_id": "hello-world"}}

    index = export_audit_dossiers(
        [record], repo_root=root, output_dir=tmp_path / "out", trial_facets={"hello-world": facet}
    )
    (entry,) = json.loads(index.read_text())["dossiers"]
    dossier = json.loads(Path(entry["path"]).read_text())
    assert dossier["trials"] == [
        {**facet["trials"][0], "audited_package_relation": "unknown"}
    ]


def test_dossier_combiner_reports_digest_mismatch_without_relabeling(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pkg = _package(root / "sources", "harbor/hello-world", "Say hello.\n")
    record = _record(pkg, verdict="discard")
    facet = {
        "trials": [{"job": "j", "trial": "t", "task_digest": DIGEST_A}],
        "coverage": {"task_id": "hello-world"},
    }
    dossier = dossier_for_audit(record, facet)
    (trial,) = dossier["trials"]
    assert trial["audited_package_relation"] == "mismatch"
    assert "verdict" not in trial or trial.get("verdict") != "discard"
    assert dossier["audit"]["verdict"] == "discard"


def test_dossier_combiner_none_means_not_collected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pkg = _package(root / "sources", "harbor/hello-world", "Say hello.\n")
    dossier = dossier_for_audit(_record(pkg))
    assert dossier["trials"] is None
    assert dossier["coverage"]["collected"] is False
    json.dumps(dossier)
