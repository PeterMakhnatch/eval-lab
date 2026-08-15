"""ProvenanceMetadata contract tests.

Deterministic per agents/CHECKS.md: fixed timestamps, no host state, no I/O.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evallab.schemas import ProvenanceMetadata

DIGEST = "sha256:" + "a" * 64
PARENT = "sha256:" + "b" * 64
WHEN = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def base(**overrides):
    payload = {
        "item_id": "mcp-agent-trajectory-benchmark@1f2e3d4c5b6a",
        "zone": "01-external",
        "source_uri": "https://huggingface.co/datasets/obaydata/mcp-agent-trajectory-benchmark",
        "revision": "1f2e3d4c5b6a" + "0" * 28,
        "material_digest": DIGEST,
        "license": "apache-2.0",
        "created_at": WHEN,
        "created_by": "evallab-fetch",
    }
    payload.update(overrides)
    return payload


def test_zone01_external_valid_roundtrip():
    item = ProvenanceMetadata.model_validate(base())
    again = ProvenanceMetadata.model_validate_json(item.model_dump_json())
    assert again == item
    assert again.zone == "01-external"
    assert again.transform is None


def test_zone01_requires_revision_pin():
    with pytest.raises(ValidationError, match="immutable revision pin"):
        ProvenanceMetadata.model_validate(base(revision=None))


def test_zone02_local_evidence_needs_no_pin_or_transform():
    item = ProvenanceMetadata.model_validate(
        base(
            zone="02-local-evidence",
            source_uri="runs/canary-event-summary-codex-20260814",
            revision=None,
            license=None,
        )
    )
    assert item.parent_digests == []


def test_zone03_synthetic_requires_versioned_transform():
    with pytest.raises(ValidationError, match="require a transform"):
        ProvenanceMetadata.model_validate(base(zone="03-synthetic", revision=None))
    item = ProvenanceMetadata.model_validate(
        base(zone="03-synthetic", revision=None, transform="taskgen@0.1.0")
    )
    assert item.transform == "taskgen@0.1.0"


def test_zone04_curated_requires_parent_lineage():
    with pytest.raises(ValidationError, match="cite parent digests"):
        ProvenanceMetadata.model_validate(
            base(zone="04-curated", revision=None, transform="sft-distill@0.1.0")
        )
    item = ProvenanceMetadata.model_validate(
        base(
            zone="04-curated",
            revision=None,
            transform="sft-distill@0.1.0",
            parent_digests=[PARENT],
        )
    )
    assert item.parent_digests == [PARENT]


def test_transform_must_be_name_at_version():
    with pytest.raises(ValidationError, match="name@version"):
        ProvenanceMetadata.model_validate(
            base(zone="03-synthetic", revision=None, transform="taskgen")
        )


def test_parent_digests_must_be_sha256():
    with pytest.raises(ValidationError, match="not sha256-formatted"):
        ProvenanceMetadata.model_validate(base(parent_digests=["md5:abc"]))


def test_material_digest_pattern_enforced():
    with pytest.raises(ValidationError):
        ProvenanceMetadata.model_validate(base(material_digest="deadbeef"))


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        ProvenanceMetadata.model_validate(base(surprise="field"))
