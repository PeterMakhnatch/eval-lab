"""Synthetic trace-store identity contract."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from evallab.trace_store.core import trace_uuid


def test_uuid_contract_is_shared_stable_and_namespaced() -> None:
    expected = str(uuid5(NAMESPACE_URL, "evallab-trace-store/v1:synthetic-trace"))
    assert trace_uuid("synthetic-trace") == expected
    assert UUID(expected).version == 5
    assert trace_uuid("other-trace") != expected
