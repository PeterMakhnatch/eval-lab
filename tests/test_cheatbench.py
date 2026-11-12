"""Raw-trace parsing and field-path tests for ``evallab.cheatbench``."""

from __future__ import annotations

import pytest

from evallab.cheatbench import (
    CheatbenchError,
    parse_raw_trace,
    resolve_field_path,
)


def test_resolve_field_path_nested_and_failure():
    obj = {"messages": [{"content": "hi"}, {"content": "there"}]}
    assert resolve_field_path(obj, "messages[1].content") == "there"
    with pytest.raises(LookupError):
        resolve_field_path(obj, "messages[7].content")
    with pytest.raises(LookupError):
        resolve_field_path(obj, "")


def test_parse_raw_trace_schema_policy():
    assert parse_raw_trace('{"id": "x", "messages": []}')["id"] == "x"
    assert parse_raw_trace('{"schema_version": "cheatbench.raw_trace.v1"}') == {
        "schema_version": "cheatbench.raw_trace.v1"
    }
    with pytest.raises(CheatbenchError):
        parse_raw_trace('{"schema_version": "something.else.v9"}')
    with pytest.raises(CheatbenchError):
        parse_raw_trace("not json")
