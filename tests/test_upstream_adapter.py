from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab.atif import _validate_fallback
from evallab.upstream_adapter import (
    AdapterRefusal,
    import_upstream_file,
    load_adapter_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = REPO_ROOT / "library" / "adapters"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "upstream_adapters"
CASES = (
    (
        "exgentic",
        FIXTURES / "exgentic" / "trajectory.jsonl",
        ADAPTERS / "exgentic" / "adapter-manifest.json",
        "Apache-2.0",
    ),
    (
        "recovery-bench",
        FIXTURES / "recovery_bench" / "result.json",
        ADAPTERS / "recovery-bench" / "adapter-manifest.json",
        "MIT",
    ),
)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _revision(manifest: Path) -> str:
    return json.loads(manifest.read_text())["upstream"]["revision"]


@pytest.mark.parametrize("name,source,manifest,license_id", CASES)
def test_file_import_is_byte_identical_and_raw_revision_bound(
    tmp_path: Path, name: str, source: Path, manifest: Path, license_id: str
) -> None:
    revision = _revision(manifest)
    first = import_upstream_file(
        source,
        tmp_path / "first",
        manifest,
        REPO_ROOT,
        source_root=FIXTURES,
        source_revision=revision,
        accepted_licenses=frozenset({license_id}),
    )
    second = import_upstream_file(
        source,
        tmp_path / "second",
        manifest,
        REPO_ROOT,
        source_root=FIXTURES,
        source_revision=revision,
        accepted_licenses=frozenset({license_id}),
    )
    assert _tree_bytes(first.destination) == _tree_bytes(second.destination)
    assert first.raw_path.read_bytes() == source.read_bytes()
    evidence = json.loads(first.evidence_path.read_bytes())
    assert evidence["source"]["raw_digest"] == (
        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert evidence["source"]["revision"] == revision
    if first.atif_path is None:
        assert name == "recovery-bench"
        assert evidence["trajectory"] is None
        assert evidence["adapter"]["output"]["format"] == "external-evidence"
        assert not (first.destination / "atif").exists()
    else:
        assert name == "exgentic"
        assert evidence["trajectory"]["schema_version"] == "ATIF-v1.7"
        assert _validate_fallback(json.loads(first.atif_path.read_bytes())) is None
    assert evidence["mapping"]["unknown_field_policy"] == (
        "retained-in-evidence-not-mapped-to-atif"
    )
    assert evidence["mapping"]["unknown_fields_by_record"][0]["fixture_note"].startswith(
        "constructed"
    )


@pytest.mark.parametrize("_name,source,manifest,license_id", CASES)
def test_revision_and_license_are_refusal_boundaries(
    tmp_path: Path, _name: str, source: Path, manifest: Path, license_id: str
) -> None:
    kwargs = {
        "source_root": FIXTURES,
        "source_revision": _revision(manifest),
        "accepted_licenses": frozenset({license_id}),
    }
    with pytest.raises(AdapterRefusal, match="revision mismatch"):
        import_upstream_file(
            source,
            tmp_path / "revision",
            manifest,
            REPO_ROOT,
            **{**kwargs, "source_revision": "0" * 40},
        )
    with pytest.raises(AdapterRefusal, match="license not accepted"):
        import_upstream_file(
            source,
            tmp_path / "license",
            manifest,
            REPO_ROOT,
            **{**kwargs, "accepted_licenses": frozenset()},
        )


def _temporary_manifest(
    tmp_path: Path, original: Path, *, fixture: Path, mutate: callable
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    copied_fixture = root / "fixture" / fixture.name
    copied_fixture.parent.mkdir(parents=True)
    copied_fixture.write_bytes(fixture.read_bytes())
    payload = json.loads(original.read_text())
    payload["compatibility_fixture"] = {
        "path": f"fixture/{fixture.name}",
        "digest": "sha256:" + hashlib.sha256(copied_fixture.read_bytes()).hexdigest(),
    }
    mutate(payload)
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(payload))
    return root, manifest


def test_schema_version_and_fixture_drift_are_refused(tmp_path: Path) -> None:
    fixture = FIXTURES / "exgentic" / "trajectory.jsonl"
    original = ADAPTERS / "exgentic" / "adapter-manifest.json"
    root, manifest = _temporary_manifest(
        tmp_path,
        original,
        fixture=fixture,
        mutate=lambda value: value["input"].update(version="v2"),
    )
    with pytest.raises(AdapterRefusal, match="incompatible input schema"):
        import_upstream_file(
            fixture,
            tmp_path / "out",
            manifest,
            root,
            source_root=FIXTURES,
            source_revision=_revision(original),
            accepted_licenses=frozenset({"Apache-2.0"}),
        )
    root, manifest = _temporary_manifest(
        tmp_path / "drift", original, fixture=fixture, mutate=lambda _value: None
    )
    (root / "fixture" / fixture.name).write_bytes(b"drift")
    with pytest.raises(AdapterRefusal, match="fixture drift"):
        load_adapter_manifest(manifest, root)


def test_malformed_unknown_and_path_sources_are_refused(tmp_path: Path) -> None:
    manifest = ADAPTERS / "exgentic" / "adapter-manifest.json"
    revision = _revision(manifest)
    source_root = tmp_path / "sources"
    source_root.mkdir()
    malformed = source_root / "bad.jsonl"
    malformed.write_bytes(b"not json\n")
    with pytest.raises(AdapterRefusal, match="malformed Exgentic"):
        import_upstream_file(
            malformed,
            tmp_path / "bad-out",
            manifest,
            REPO_ROOT,
            source_root=source_root,
            source_revision=revision,
            accepted_licenses=frozenset({"Apache-2.0"}),
        )
    unsupported = source_root / "unknown.jsonl"
    unsupported.write_text(
        json.dumps(
            {
                "event": "invented",
                "step": 0,
                "session_id": "s",
                "task_id": "t",
            }
        )
        + "\n"
    )
    with pytest.raises(AdapterRefusal, match="unsupported event"):
        import_upstream_file(
            unsupported,
            tmp_path / "unknown-out",
            manifest,
            REPO_ROOT,
            source_root=source_root,
            source_revision=revision,
            accepted_licenses=frozenset({"Apache-2.0"}),
        )
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes((FIXTURES / "exgentic" / "trajectory.jsonl").read_bytes())
    with pytest.raises(AdapterRefusal, match="escapes"):
        import_upstream_file(
            outside,
            tmp_path / "outside-out",
            manifest,
            REPO_ROOT,
            source_root=source_root,
            source_revision=revision,
            accepted_licenses=frozenset({"Apache-2.0"}),
        )


def test_manifest_unknown_fields_and_unsafe_fixture_paths_are_refused(tmp_path: Path) -> None:
    fixture = FIXTURES / "exgentic" / "trajectory.jsonl"
    original = ADAPTERS / "exgentic" / "adapter-manifest.json"
    root, manifest = _temporary_manifest(
        tmp_path,
        original,
        fixture=fixture,
        mutate=lambda value: value.update(unreviewed=True),
    )
    with pytest.raises(AdapterRefusal, match="unknown fields"):
        load_adapter_manifest(manifest, root)
    root, manifest = _temporary_manifest(
        tmp_path / "path",
        original,
        fixture=fixture,
        mutate=lambda value: value["compatibility_fixture"].update(path="../escape.jsonl"),
    )
    with pytest.raises(AdapterRefusal, match="repository-relative"):
        load_adapter_manifest(manifest, root)


def test_adapter_code_digest_drift_is_refused(tmp_path: Path) -> None:
    fixture = FIXTURES / "exgentic" / "trajectory.jsonl"
    original = ADAPTERS / "exgentic" / "adapter-manifest.json"
    root, manifest = _temporary_manifest(
        tmp_path,
        original,
        fixture=fixture,
        mutate=lambda value: value.update(adapter_code_digest=f"sha256:{'0' * 64}"),
    )
    with pytest.raises(AdapterRefusal, match="adapter code drift"):
        load_adapter_manifest(manifest, root)


def test_role_and_capabilities_must_match_the_input_contract(tmp_path: Path) -> None:
    exgentic_fixture = FIXTURES / "exgentic" / "trajectory.jsonl"
    exgentic_manifest = ADAPTERS / "exgentic" / "adapter-manifest.json"
    root, manifest = _temporary_manifest(
        tmp_path / "role",
        exgentic_manifest,
        fixture=exgentic_fixture,
        mutate=lambda value: value.update(role="result"),
    )
    with pytest.raises(AdapterRefusal, match="claims do not match"):
        load_adapter_manifest(manifest, root)

    recovery_fixture = FIXTURES / "recovery_bench" / "result.json"
    recovery_manifest = ADAPTERS / "recovery-bench" / "adapter-manifest.json"
    root, manifest = _temporary_manifest(
        tmp_path / "capabilities",
        recovery_manifest,
        fixture=recovery_fixture,
        mutate=lambda value: value.update(capabilities=["trajectory-events"]),
    )
    with pytest.raises(AdapterRefusal, match="claims do not match"):
        load_adapter_manifest(manifest, root)
