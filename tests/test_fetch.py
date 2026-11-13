from __future__ import annotations

import pytest

from evallab.fetch import (
    FetchError,
    parse_pin,
)


def test_parse_pin_refuses_latest_and_bare_name() -> None:
    with pytest.raises(FetchError, match="@latest"):
        parse_pin("aime@latest")
    with pytest.raises(FetchError, match="unpinned"):
        parse_pin("aime")
    with pytest.raises(FetchError, match="unpinned"):
        parse_pin("ds-1000@head")
    pin = parse_pin("hello-world@1.0")
    assert pin.ref == "hello-world@1.0"
