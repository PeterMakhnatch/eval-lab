"""Public CLI recovery refuses ambiguous or unattributed operator actions."""

from __future__ import annotations

import pytest

from evallab.cli import parser


@pytest.mark.parametrize(
    "resolution",
    [
        ["--action", "retry"],
        ["--actor", "operator"],
        ["--action", "discard", "--actor", "operator"],
    ],
)
def test_ambiguous_recovery_requires_explicit_action_and_attribution(resolution) -> None:
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["analyze", "worker-resolve-ambiguous", "request-id", *resolution])
    assert error.value.code == 2
