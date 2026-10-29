"""Source-native actor projections for issue-bank detector replay.

Adapters are pure readers: no label metadata, network access or actor execution.
A projection preserves captured actor text, reasoning, calls and feedback, and
states its capture scope instead of inventing missing interaction history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

CaptureScope = Literal["source_native_run", "partial_actor_history", "single_response"]


class ActorUnavailable(ValueError):
    """The source has no supported, attributable actor history."""


@dataclass(frozen=True)
class ActorProjection:
    """An ATIF-shaped actor-only projection plus private source-step bindings."""

    doc: dict[str, Any]
    scope: CaptureScope
    origins: Mapping[int, str | int]
    limitations: tuple[str, ...] = ()
