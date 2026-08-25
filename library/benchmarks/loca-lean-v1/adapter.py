"""Harbor-facing adapter: one source-digest-addressed LOCA canary."""
from __future__ import annotations

try:
    from .materializer import materialize, output_path, reject_committed_corpora
except ImportError:  # direct execution from this directory
    from materializer import materialize, output_path, reject_committed_corpora

__all__ = ["materialize", "output_path", "reject_committed_corpora"]
