"""Collection identity and privacy guards for the retained Docent reader."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from evallab.trace_store import docent

DATASET = "mimo-defect-map-v1"
COLLECTION_ID = "collection-private"
OWNER_ID = "owner-user"


def _details(dataset: str = DATASET, identity: str = COLLECTION_ID) -> dict[str, Any]:
    return {
        "id": identity, "name": docent.collection_name(dataset), "created_by": OWNER_ID,
        "owner": {"id": OWNER_ID}, "is_owner": True, "my_permission": "admin",
    }


def _acl() -> list[dict[str, Any]]:
    return [{"collection_id": COLLECTION_ID, "subject_type": "user", "subject_id": OWNER_ID,
             "subject": {"id": OWNER_ID}, "permission_level": "admin"}]


class _Client:
    def __init__(self, collections: Any = None) -> None:
        self.collections = [_details()] if collections is None else collections
        self.details: Any = _details()
        self.acl: Any = _acl()
        self.created: list[str] = []

    def list_collections(self) -> Any:
        return copy.deepcopy(self.collections)

    def create_collection(self, *, name: str) -> str:
        self.created.append(name)
        return COLLECTION_ID

    def get_collection(self, collection_id: str) -> Any:
        assert collection_id == COLLECTION_ID
        return copy.deepcopy(self.details)

    def get_collection_collaborators(self, collection_id: str) -> Any:
        assert collection_id == COLLECTION_ID
        return copy.deepcopy(self.acl)


def test_owner_only_collection_returns_the_authenticated_owner() -> None:
    client = _Client()
    collection = docent.resolve_collections(client, dataset=DATASET, create=False)[0]
    assert docent.require_owner_only(client, collection) == OWNER_ID
    assert client.created == []


@pytest.mark.parametrize("acl", [
    None, [], {}, [None],
    [{"subject_type": "public", "permission_level": "read"}],
    [{"subject_type": "organization", "subject_id": OWNER_ID, "permission_level": "admin"}],
    [{"subject_type": "user", "subject_id": "someone-else", "permission_level": "admin"}],
    [*_acl(), {"subject_type": "user", "subject_id": "other", "permission_level": "read"}],
])
def test_unknown_or_shared_privacy_refuses_the_reader(acl: Any) -> None:
    client = _Client()
    client.acl = acl
    with pytest.raises(docent.DocentPrivacyError):
        docent.require_owner_only(client, _details())


@pytest.mark.parametrize("field,value", [
    ("is_owner", False), ("is_owner", None), ("my_permission", "write"),
    ("created_by", "different-owner"), ("owner", None), ("name", "private-looking-name"),
])
def test_unknown_collection_owner_refuses_the_reader(field: str, value: Any) -> None:
    client = _Client()
    client.details[field] = value
    with pytest.raises(docent.DocentPrivacyError):
        docent.require_owner_only(client, _details())


def test_collection_resolution_rejects_duplicate_exact_names() -> None:
    duplicate = _Client(collections=[_details(), _details(identity="duplicate")])
    with pytest.raises(docent.DocentPrivacyError, match="Duplicate exact"):
        docent.resolve_collections(duplicate, dataset=DATASET, create=False)
    assert duplicate.created == []


def test_collection_resolution_never_uses_a_similar_name() -> None:
    client = _Client(collections=[{"id": "unrelated", "name": "old " + docent.collection_name(DATASET)}])
    with pytest.raises(ValueError, match="No exact"):
        docent.resolve_collections(client, dataset=DATASET, create=False)
    assert client.created == []
    assert docent.resolve_collections(client, dataset=DATASET, create=True) == [
        {"id": COLLECTION_ID, "name": docent.collection_name(DATASET), "dataset": DATASET},
    ]
    assert client.created == [docent.collection_name(DATASET)]


def test_sdk_pin_checked_on_actual_client_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docent, "version", lambda name: "0.1.87")
    with pytest.raises(RuntimeError, match="docent==0.1.88"):
        docent.new_client()
