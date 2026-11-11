"""Pinned Docent client and collection privacy guards for retained readers.

Gold owns local labelled trajectories. Remote replica ingestion is retired.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from evallab.trace_store.core import ALLOWED_DATASETS

SDK_VERSION = "0.1.88"


class DocentPrivacyError(RuntimeError):
    """Collection ownership or its complete owner-only ACL cannot be proven."""


def _require_sdk() -> None:
    try:
        installed = version("docent")
    except PackageNotFoundError:
        raise RuntimeError("Docent trace storage requires optional docent==0.1.88") from None
    if installed != SDK_VERSION:
        raise RuntimeError("Docent trace storage requires optional docent==0.1.88")


def new_client() -> Any:
    """Construct only the storage/query client; never schedule readings or a UI."""
    _require_sdk()
    from docent import Docent  # ty: ignore[unresolved-import]

    client = Docent(log_stream=sys.stderr)
    client.auto_flush = False
    return client


def collection_name(dataset: str) -> str:
    if dataset not in ALLOWED_DATASETS:
        raise ValueError("Dataset is not admitted to the private trace store")
    return f"evallab-trace-store/{dataset} (private)"


def resolve_collections(
    client: Any, *, dataset: str | None = None, create: bool = False,
) -> list[dict[str, Any]]:
    """Resolve only exact allowlisted names, rejecting ambiguous collections."""
    datasets = [dataset] if dataset is not None else sorted(ALLOWED_DATASETS)
    names = {collection_name(source): source for source in datasets}
    listed = client.list_collections()
    if not isinstance(listed, list) or any(not isinstance(row, Mapping) for row in listed):
        raise DocentPrivacyError("Cannot resolve Docent collection names")
    result = []
    for name, source in names.items():
        matches = [dict(row) for row in listed if row.get("name") == name]
        if len(matches) > 1:
            raise DocentPrivacyError(f"Duplicate exact Docent collection name: {name}")
        if not matches and create:
            collection_id = client.create_collection(name=name)
            if not isinstance(collection_id, str) or not collection_id:
                raise DocentPrivacyError("Docent did not return a collection ID")
            matches = [{"id": collection_id, "name": name}]
        if matches:
            row = matches[0]
            if not isinstance(row.get("id"), str) or not row["id"]:
                raise DocentPrivacyError("Docent collection has no valid ID")
            result.append({"id": row["id"], "name": name, "dataset": source})
    if dataset is not None and not result:
        raise ValueError("No exact private Docent collection exists for the dataset")
    return result


def require_owner_only(client: Any, collection: Mapping[str, Any]) -> str:
    """Require the authenticated owner and exactly their single user-admin ACL."""
    collection_id = collection["id"]
    details = client.get_collection(collection_id)
    collaborators = client.get_collection_collaborators(collection_id)
    if not isinstance(details, Mapping):
        raise DocentPrivacyError("Docent collection ownership is unknown")
    owner = details.get("owner")
    owner_id = owner.get("id") if isinstance(owner, Mapping) else None
    if (
        details.get("id") != collection_id
        or details.get("name") != collection["name"]
        or details.get("is_owner") is not True
        or details.get("my_permission") != "admin"
        or not isinstance(owner_id, str)
        or not owner_id
        or details.get("created_by") != owner_id
    ):
        raise DocentPrivacyError("Docent collection is not proven to belong to the authenticated owner")
    if not isinstance(collaborators, list) or len(collaborators) != 1:
        raise DocentPrivacyError("Docent collection ACL is unknown or shared")
    collaborator = collaborators[0]
    if (
        not isinstance(collaborator, Mapping)
        or collaborator.get("subject_type") != "user"
        or collaborator.get("subject_id") != owner_id
        or collaborator.get("permission_level") != "admin"
        or collaborator.get("collection_id", collection_id) != collection_id
    ):
        raise DocentPrivacyError("Docent collection ACL is not owner-only")
    return owner_id


