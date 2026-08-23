"""Repo-owned Harbor adapter that pins a complete Codex CLI release."""

from __future__ import annotations

from typing import Any

from harbor.agents.installed.codex import Codex  # ty: ignore[unresolved-import]

# 0.149.0 was published before its linux-arm64 optional package. Harbor's
# `@latest` installer therefore installed an unusable CLI on arm64 task images.
# Pin the newest release whose base and linux-arm64 packages are both present.
PINNED_CODEX_VERSION = "0.148.0"


class PinnedCodex(Codex):
    """Use a reproducible Codex CLI version with a published arm64 binary."""

    def __init__(
        self,
        *args: Any,
        version: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            version=version or PINNED_CODEX_VERSION,
            **kwargs,
        )
