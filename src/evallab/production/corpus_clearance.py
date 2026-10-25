"""Fail-closed, identity-only clearance for locally staged corpus records.

A caller-pinned manifest is trusted policy input; it is not independently
authenticated publisher approval. This module deliberately loads no corpus
registry, answer keys, dataset bodies, database state, or network resources.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

SCHEMA = "evallab.corpus_clearance/v1"
_USES = (
    "operator_raw",
    "actor_retrieval",
    "scorer",
    "derivatives",
    "training",
    "redistribution",
)
_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")
_RECORD_FIELDS = frozenset(
    {
        "source_dataset",
        "source_revision",
        "native_id",
        "aliases",
        "canonical_identity",
        "family_id",
        "effective_split",
        "split_authority",
        "rights",
        "reasons",
    }
)
_CANONICAL_FIELDS = ("dataset_id", "item_id", "task_id")
_SPLITS = frozenset({"dev", "test", "held", "unknown", "conflict"})
_DECISIONS = frozenset({"allow", "deny", "unknown"})
_UNRESOLVED = frozenset({"unknown", "conflict"})
IdentityKey = tuple[str, str, str]


class ClearanceDenied(ValueError):
    """The manifest or requested use does not establish clearance."""


@dataclass(frozen=True, slots=True)
class Authority:
    ref: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    dataset_id: str
    item_id: str
    task_id: str


@dataclass(frozen=True, slots=True)
class RightGrant:
    decision: str
    authority: Authority | None


@dataclass(frozen=True, slots=True)
class ClearanceRecord:
    source_dataset: str
    source_revision: str
    native_id: str
    aliases: tuple[str, ...]
    canonical_identity: CanonicalIdentity | None
    family_id: str | None
    effective_split: str
    split_authority: Authority | None
    rights: Mapping[str, RightGrant]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClearanceManifest:
    """An immutable snapshot of validated, caller-pinned identity policy."""

    sha256: str
    records: tuple[ClearanceRecord, ...]
    _identities: Mapping[IdentityKey, ClearanceRecord] = field(repr=False)

    @classmethod
    def load(cls, path: str | Path, *, expected_sha256: str) -> ClearanceManifest:
        """Load exactly the pinned bytes, rejecting ambiguous or malformed policy.

        Unresolved records may be retained as held policy, but cannot pass
        :meth:`require`. Digests attest which policy bytes the caller selected;
        they do not authenticate the publisher or the referenced authorities.
        """
        expected = _digest_text(expected_sha256, "expected manifest SHA-256")
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise ClearanceDenied("clearance manifest cannot be read") from exc
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise ClearanceDenied("clearance manifest SHA-256 does not match the pin")
        try:
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ClearanceDenied) as exc:
            raise ClearanceDenied("clearance manifest is not valid strict JSON") from exc
        _exact_keys(document, {"schema", "records"}, "manifest")
        if document["schema"] != SCHEMA:
            raise ClearanceDenied("unsupported clearance manifest schema")
        raw_records = document["records"]
        if not isinstance(raw_records, list):
            raise ClearanceDenied("clearance records must be a list")

        records: list[ClearanceRecord] = []
        identities: dict[IdentityKey, ClearanceRecord] = {}
        canonical_items: set[tuple[str, str]] = set()
        canonical_tasks: set[str] = set()
        for index, raw_record in enumerate(raw_records):
            record = _record(raw_record, index)
            dataset = _identity_text(record.source_dataset, "source_dataset")
            revision = _identity_text(record.source_revision, "source_revision")
            # Publisher aliases are unresolved hints, not additional authority keys.
            # Multiple held observations may legitimately share an alias; only the
            # exact source-native coordinate selects a policy record.
            identity = (dataset, revision, _identity_text(record.native_id, "native identity"))
            if identity in identities:
                raise ClearanceDenied("duplicate or conflicting normalized source identity")
            identities[identity] = record
            if record.canonical_identity is not None:
                canonical_dataset, canonical_item, canonical_task = _canonical_key(
                    record.canonical_identity
                )
                item_key = (canonical_dataset, canonical_item)
                if item_key in canonical_items or canonical_task in canonical_tasks:
                    raise ClearanceDenied("duplicate or conflicting normalized canonical identity")
                canonical_items.add(item_key)
                canonical_tasks.add(canonical_task)
            records.append(record)

        return cls(
            sha256=actual,
            records=tuple(records),
            _identities=MappingProxyType(identities),
        )

    def require(
        self,
        *,
        source_dataset: str,
        source_revision: str,
        native_id: str,
        use: str = "operator_raw",
    ) -> ClearanceRecord:
        """Return an independently cleared DEV record or deny the requested use.

        Lookup reads only this already-loaded snapshot. Public availability and
        source identity metadata never substitute for canonical, family, split,
        and use authority. Split authority attests the manifest's canonical
        family/split assignment; this module does not fetch or authenticate it.
        """
        if not isinstance(use, str) or use not in _USES:
            raise ClearanceDenied("unknown clearance use")
        key = (
            _identity_text(source_dataset, "source_dataset"),
            _identity_text(source_revision, "source_revision"),
            _identity_text(native_id, "native_id"),
        )
        record = self._identities.get(key)
        if record is None:
            raise ClearanceDenied("source identity has no clearance record")
        if record.canonical_identity is None or any(
            value in _UNRESOLVED for value in _canonical_key(record.canonical_identity)
        ):
            raise ClearanceDenied("canonical identity authority is missing or unresolved")
        if record.family_id is None or record.family_id.strip().casefold() in _UNRESOLVED:
            raise ClearanceDenied("canonical family authority is missing or unresolved")
        if record.effective_split != "dev":
            raise ClearanceDenied("effective split is not DEV")
        _require_authority(record.split_authority, "split")
        grant = record.rights[use]
        if grant.decision != "allow":
            raise ClearanceDenied(f"{use} is not allowed")
        _require_authority(grant.authority, use)
        return record


def _digest_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ClearanceDenied(f"{label} must be a hexadecimal SHA-256 digest")
    return value.lower()


def _identity_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClearanceDenied(f"{label} must be a non-empty string")
    return value.strip().casefold()


def _exact_keys(value: Any, expected: set[str] | frozenset[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ClearanceDenied(f"{label} does not have the exact clearance fields")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClearanceDenied("duplicate JSON object field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ClearanceDenied(f"non-standard JSON constant: {value}")


def _authority(value: Any, label: str) -> Authority | None:
    if value is None:
        return None
    _exact_keys(value, {"ref", "sha256"}, label)
    ref = value["ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise ClearanceDenied(f"{label} reference must be a non-empty string")
    return Authority(ref=ref, sha256=_digest_text(value["sha256"], f"{label} SHA-256"))


def _require_authority(value: Authority | None, label: str) -> None:
    if value is None or value.ref.strip().casefold() in _UNRESOLVED:
        raise ClearanceDenied(f"{label} authority is missing or unresolved")


def _record(value: Any, index: int) -> ClearanceRecord:
    _exact_keys(value, _RECORD_FIELDS, f"record {index}")
    for field_name in ("source_dataset", "source_revision", "native_id"):
        _identity_text(value[field_name], field_name)

    raw_aliases = value["aliases"]
    if not isinstance(raw_aliases, list):
        raise ClearanceDenied("record aliases must be a list")
    for alias in raw_aliases:
        _identity_text(alias, "record alias")

    raw_canonical = value["canonical_identity"]
    canonical: CanonicalIdentity | None
    if raw_canonical is None:
        canonical = None
    else:
        _exact_keys(raw_canonical, frozenset(_CANONICAL_FIELDS), "canonical identity")
        for key in _CANONICAL_FIELDS:
            _identity_text(raw_canonical[key], f"canonical {key}")
        canonical = CanonicalIdentity(**raw_canonical)

    family_id = value["family_id"]
    if family_id is not None and (not isinstance(family_id, str) or not family_id.strip()):
        raise ClearanceDenied("family_id must be null or a non-empty string")
    effective_split = value["effective_split"]
    if not isinstance(effective_split, str) or effective_split not in _SPLITS:
        raise ClearanceDenied("unknown effective split")
    split_authority = _authority(value["split_authority"], "split authority")

    raw_rights = value["rights"]
    _exact_keys(raw_rights, frozenset(_USES), "rights")
    rights: dict[str, RightGrant] = {}
    for use in _USES:
        raw_grant = raw_rights[use]
        _exact_keys(raw_grant, {"decision", "authority"}, f"{use} grant")
        decision = raw_grant["decision"]
        if not isinstance(decision, str) or decision not in _DECISIONS:
            raise ClearanceDenied("unknown rights decision")
        rights[use] = RightGrant(
            decision=decision,
            authority=_authority(raw_grant["authority"], f"{use} authority"),
        )

    reasons = value["reasons"]
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise ClearanceDenied("record reasons must be a list of strings")
    return ClearanceRecord(
        source_dataset=value["source_dataset"],
        source_revision=value["source_revision"],
        native_id=value["native_id"],
        aliases=tuple(raw_aliases),
        canonical_identity=canonical,
        family_id=family_id,
        effective_split=effective_split,
        split_authority=split_authority,
        rights=MappingProxyType(rights),
        reasons=tuple(reasons),
    )


def _canonical_key(identity: CanonicalIdentity) -> IdentityKey:
    return (
        _identity_text(identity.dataset_id, "canonical dataset_id"),
        _identity_text(identity.item_id, "canonical item_id"),
        _identity_text(identity.task_id, "canonical task_id"),
    )


__all__ = ["ClearanceDenied", "ClearanceManifest", "ClearanceRecord"]
