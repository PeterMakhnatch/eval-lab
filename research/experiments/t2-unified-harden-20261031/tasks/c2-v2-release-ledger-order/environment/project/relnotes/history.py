"""Reader for the migrated release-history export.

The export lives at ``data/releases.tsv``; ``data/README.md`` documents the
column layout and why row order is authoritative.
"""

from __future__ import annotations

import os


def release_ids(
    channel: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return release ids in export row order, optionally filtered by channel."""
    raise NotImplementedError("release_ids is not implemented yet")
