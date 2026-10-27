"""Synthetic admission and independent-use failures; no corpus bodies are loaded."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from evallab.production.corpus_clearance import ClearanceDenied, ClearanceManifest

_USES = (
    "operator_raw",
    "actor_retrieval",
    "scorer",
    "derivatives",
    "training",
    "redistribution",
)
_AUTH_SHA = hashlib.sha256(b"synthetic identity policy authority").hexdigest()
_IDENTITY = {
    "source_dataset": "synthetic-public",
    "source_revision": "revision-a",
    "native_id": "item-a",
}


def _authority() -> dict[str, str]:
    return {"ref": "synthetic:policy-v1", "sha256": _AUTH_SHA}


def _record(*, allowed: tuple[str, ...] = ("operator_raw",)) -> dict[str, Any]:
    return {
        **_IDENTITY,
        "aliases": ["alias-a"],
        "canonical_identity": {
            "dataset_id": "canonical-dataset",
            "item_id": "canonical-item-a",
            "task_id": "canonical-task-a",
        },
        "family_id": "synthetic-family",
        "effective_split": "dev",
        "split_authority": _authority(),
        "rights": {
            use: {
                "decision": "allow" if use in allowed else "unknown",
                "authority": _authority() if use in allowed else None,
            }
            for use in _USES
        },
        "reasons": ["Synthetic identity metadata only."],
    }


def _document(*records: dict[str, Any]) -> dict[str, Any]:
    return {"schema": "evallab.corpus_clearance/v1", "records": list(records)}


def _write(tmp_path: Path, document: dict[str, Any]) -> tuple[Path, str]:
    raw = json.dumps(document, sort_keys=True).encode("utf-8")
    path = tmp_path / "clearance.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _load(tmp_path: Path, *records: dict[str, Any]) -> ClearanceManifest:
    path, digest = _write(tmp_path, _document(*records))
    return ClearanceManifest.load(path, expected_sha256=digest)


def test_pin_covers_exact_bytes_and_loaded_decisions_are_a_snapshot(tmp_path: Path) -> None:
    path, digest = _write(tmp_path, _document(_record()))
    manifest = ClearanceManifest.load(path, expected_sha256=digest.upper())
    assert manifest.sha256 == digest

    # Semantically identical policy is not the exact evidence the caller pinned.
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ClearanceDenied):
        ClearanceManifest.load(path, expected_sha256=digest)

    # Later removal of the policy file cannot cause a hidden filesystem lookup.
    path.unlink()
    assert manifest.require(**_IDENTITY).effective_split == "dev"


def test_source_lookup_normalizes_coordinates_without_authorizing_publisher_aliases(
    tmp_path: Path,
) -> None:
    record = _record()
    record.update(source_dataset=" Synthetic-Public ", source_revision=" REVISION-A ")
    record["native_id"] = " Straße "
    record["aliases"] = [" Other-ID ", "STRASSE"]
    manifest = _load(tmp_path, record)
    primary = manifest.require(
        source_dataset="SYNTHETIC-PUBLIC",
        source_revision="revision-a",
        native_id="STRASSE",
    )
    assert primary.canonical_identity is not None
    assert primary.canonical_identity.task_id == "canonical-task-a"
    with pytest.raises(ClearanceDenied):
        manifest.require(**{**_IDENTITY, "native_id": "other-id"})


@pytest.mark.parametrize("field", tuple(_IDENTITY))
def test_lookup_never_falls_back_across_source_coordinates(tmp_path: Path, field: str) -> None:
    manifest = _load(tmp_path, _record())
    identity = {**_IDENTITY, field: "not-cleared"}
    with pytest.raises(ClearanceDenied):
        manifest.require(**identity)


def test_validated_policy_and_nested_grants_cannot_be_mutated(tmp_path: Path) -> None:
    manifest = _load(tmp_path, _record())
    record = manifest.require(**_IDENTITY)
    with pytest.raises(FrozenInstanceError):
        record.effective_split = "test"  # type: ignore[misc]
    with pytest.raises(TypeError):
        record.rights["training"] = record.rights["operator_raw"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        record.rights["operator_raw"].decision = "deny"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        record.aliases += ("new-alias",)  # type: ignore[misc]
    assert record.split_authority is not None
    with pytest.raises(FrozenInstanceError):
        record.split_authority.ref = "other-policy"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        manifest.sha256 = "0" * 64  # type: ignore[misc]
    with pytest.raises(ClearanceDenied):
        manifest.require(**{**_IDENTITY, "native_id": "new-alias"})


@pytest.mark.parametrize("allowed_use", _USES)
def test_every_use_requires_its_own_grant(tmp_path: Path, allowed_use: str) -> None:
    record = _record(allowed=(allowed_use,))
    for index, use in enumerate(_USES):
        if use != allowed_use and index % 2:
            record["rights"][use] = {"decision": "deny", "authority": _authority()}
    manifest = _load(tmp_path, record)
    assert manifest.require(**_IDENTITY, use=allowed_use).rights[allowed_use].decision == "allow"
    for other_use in _USES:
        if other_use != allowed_use:
            with pytest.raises(ClearanceDenied):
                manifest.require(**_IDENTITY, use=other_use)


@pytest.mark.parametrize("use", _USES)
@pytest.mark.parametrize("decision,authority", [("allow", None), ("deny", True), ("unknown", True)])
def test_requested_use_needs_both_allow_and_authority(
    tmp_path: Path, use: str, decision: str, authority: bool | None
) -> None:
    record = _record(allowed=_USES)
    record["rights"][use] = {
        "decision": decision,
        "authority": _authority() if authority else None,
    }
    manifest = _load(tmp_path, record)
    with pytest.raises(ClearanceDenied):
        manifest.require(**_IDENTITY, use=use)


@pytest.mark.parametrize("split", ["test", "held", "unknown", "conflict"])
def test_even_all_rights_cannot_clear_a_non_dev_effective_split(tmp_path: Path, split: str) -> None:
    record = _record(allowed=_USES)
    record["effective_split"] = split
    manifest = _load(tmp_path, record)
    for use in _USES:
        with pytest.raises(ClearanceDenied):
            manifest.require(**_IDENTITY, use=use)


@pytest.mark.parametrize(
    "missing",
    ["canonical", "unknown-canonical", "family", "unknown-family", "split-authority", "unknown-ref"],
)
def test_source_metadata_and_rights_cannot_replace_canonical_authority(
    tmp_path: Path, missing: str
) -> None:
    record = _record(allowed=_USES)
    if missing == "canonical":
        record["canonical_identity"] = None
    elif missing == "unknown-canonical":
        record["canonical_identity"]["task_id"] = " Unknown "
    elif missing == "family":
        record["family_id"] = None
    elif missing == "unknown-family":
        record["family_id"] = " CONFLICT "
    elif missing == "split-authority":
        record["split_authority"] = None
    else:
        record["split_authority"]["ref"] = " unknown "
    manifest = _load(tmp_path, record)
    with pytest.raises(ClearanceDenied):
        manifest.require(**_IDENTITY)


def test_public_availability_does_not_grant_raw_access_or_training(tmp_path: Path) -> None:
    record = _record(allowed=())
    record["reasons"] = ["Publicly available source; no independent use grant established."]
    manifest = _load(tmp_path, record)
    for use in ("operator_raw", "training", "redistribution"):
        with pytest.raises(ClearanceDenied):
            manifest.require(**_IDENTITY, use=use)


@pytest.mark.parametrize(
    "collision", ["primary", "canonical", "canonical-item", "canonical-task", "identical"]
)
def test_conflicting_or_duplicate_normalized_identities_reject_the_manifest(
    tmp_path: Path, collision: str
) -> None:
    first = _record()
    second = deepcopy(first)
    second.update(native_id="item-b", aliases=["alias-b"], effective_split="test")
    second["canonical_identity"].update(item_id="canonical-item-b", task_id="canonical-task-b")
    if collision == "primary":
        second.update(
            source_dataset=" SYNTHETIC-PUBLIC ",
            source_revision=" REVISION-A ",
            native_id=" ITEM-A ",
        )
    elif collision == "canonical":
        second["canonical_identity"] = {
            key: f" {value.upper()} " for key, value in first["canonical_identity"].items()
        }
    elif collision == "canonical-item":
        second["canonical_identity"]["item_id"] = " CANONICAL-ITEM-A "
    elif collision == "canonical-task":
        second["canonical_identity"]["task_id"] = " CANONICAL-TASK-A "
    else:
        second = deepcopy(first)
    path, digest = _write(tmp_path, _document(first, second))
    with pytest.raises(ClearanceDenied):
        ClearanceManifest.load(path, expected_sha256=digest)


def test_alias_overlap_cannot_launder_a_held_native_identity(tmp_path: Path) -> None:
    cleared = _record()
    cleared["aliases"] = ["item-b"]
    held = _record(allowed=())
    held.update(native_id="item-b", aliases=["item-a"], canonical_identity=None,
                family_id=None, effective_split="held", split_authority=None)
    manifest = _load(tmp_path, cleared, held)
    assert manifest.require(**_IDENTITY).effective_split == "dev"
    with pytest.raises(ClearanceDenied):
        manifest.require(**{**_IDENTITY, "native_id": "item-b"})


@pytest.mark.parametrize("location", ["root", "record", "canonical", "split", "grant"])
def test_body_fields_cannot_be_smuggled_into_identity_policy(tmp_path: Path, location: str) -> None:
    record = _record()
    document = _document(record)
    targets = {
        "root": document,
        "record": record,
        "canonical": record["canonical_identity"],
        "split": record["split_authority"],
        "grant": record["rights"]["operator_raw"],
    }
    targets[location]["task_body"] = "Synthetic body that must never be accepted as identity policy."
    path, digest = _write(tmp_path, document)
    with pytest.raises(ClearanceDenied):
        ClearanceManifest.load(path, expected_sha256=digest)


@pytest.mark.parametrize("bad_digest", ["", "f" * 63, "g" * 64, " " + "f" * 64, None, False, []])
@pytest.mark.parametrize("scope", ["pin", "split", "use"])
def test_malformed_digest_is_never_authority(
    tmp_path: Path, bad_digest: Any, scope: str
) -> None:
    record = _record()
    if scope == "split":
        record["split_authority"]["sha256"] = bad_digest
    elif scope == "use":
        record["rights"]["operator_raw"]["authority"]["sha256"] = bad_digest
    path, digest = _write(tmp_path, _document(record))
    with pytest.raises(ClearanceDenied):
        ClearanceManifest.load(path, expected_sha256=bad_digest if scope == "pin" else digest)


@pytest.mark.parametrize("bad_ref", ["", "  ", None, False, []])
def test_authority_requires_a_real_reference(tmp_path: Path, bad_ref: Any) -> None:
    record = _record()
    record["rights"]["operator_raw"]["authority"]["ref"] = bad_ref
    path, digest = _write(tmp_path, _document(record))
    with pytest.raises(ClearanceDenied):
        ClearanceManifest.load(path, expected_sha256=digest)


@pytest.mark.parametrize("location", ["split", "decision"])
def test_malformed_policy_values_fail_closed(tmp_path: Path, location: str) -> None:
    record = _record()
    if location == "split":
        record["effective_split"] = []
    else:
        record["rights"]["operator_raw"]["decision"] = []
    path, digest = _write(tmp_path, _document(record))
    with pytest.raises(ClearanceDenied):
        ClearanceManifest.load(path, expected_sha256=digest)


def test_duplicate_json_fields_cannot_hide_a_conflicting_rights_decision(tmp_path: Path) -> None:
    raw = json.dumps(_document(_record())).encode("utf-8")
    raw = raw.replace(b'"decision": "allow"', b'"decision": "deny", "decision": "allow"', 1)
    path = tmp_path / "clearance.json"
    path.write_bytes(raw)
    with pytest.raises(ClearanceDenied):
        ClearanceManifest.load(path, expected_sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("use", ["public", "TRAINING", "", [], None])
def test_unrecognized_use_is_denied_even_when_all_known_uses_are_allowed(
    tmp_path: Path, use: Any
) -> None:
    manifest = _load(tmp_path, _record(allowed=_USES))
    with pytest.raises(ClearanceDenied):
        manifest.require(**_IDENTITY, use=use)
