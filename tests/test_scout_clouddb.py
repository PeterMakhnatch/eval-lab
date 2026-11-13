"""Optional real Scout integration, with only synthetic local transcripts."""

from __future__ import annotations

from pathlib import Path

from evallab.readers import ACTOR_PROJECTION_VERSION
from evallab.readers.scout_clouddb import (
    DEFAULT_CHEATBENCH_TRANSCRIPTS,
    DEFAULT_DB,
    DEFAULT_KEYMAP,
    DEFAULT_RESULTS,
    DEFAULT_SCANS,
    DEFAULT_STATE,
    DEFAULT_TRANSCRIPTS,
    TRACE_TOOLS_STATE,
    scout_paths,
)


def test_default_paths_are_projection_versioned_and_never_alias_legacy_caches() -> None:
    for path in (
        DEFAULT_STATE, DEFAULT_KEYMAP, DEFAULT_RESULTS,
        DEFAULT_TRANSCRIPTS, DEFAULT_CHEATBENCH_TRANSCRIPTS, DEFAULT_SCANS,
    ):
        assert any(ACTOR_PROJECTION_VERSION in part for part in path.parts[-2:]), path
    # The shared database and the legacy evidence roots stay unversioned and unread.
    assert DEFAULT_DB.name == "cloud-trace-db.sqlite"
    assert DEFAULT_STATE.parent.parent / "cloud-trace-db.sqlite" == DEFAULT_DB
    assert DEFAULT_KEYMAP != TRACE_TOOLS_STATE / "keymap-v1.jsonl"
    assert DEFAULT_RESULTS != TRACE_TOOLS_STATE / "results-v1.jsonl"
    assert DEFAULT_TRANSCRIPTS.name != "transcripts_clouddb_dev"
    assert DEFAULT_CHEATBENCH_TRANSCRIPTS.name != "transcripts_cheatbench_dev_blind"
    assert DEFAULT_SCANS.name != "trace-tools"
    assert DEFAULT_KEYMAP.parent == DEFAULT_RESULTS.parent == DEFAULT_STATE

    custom = scout_paths(Path("/tmp/trace-tools"), Path("/tmp/viewer"), projection_version="unit-v9")
    assert custom.keymap == Path("/tmp/trace-tools/scout-unit-v9/keymap-v1.jsonl")
    assert custom.transcripts == Path("/tmp/viewer/data/transcripts_clouddb_dev-unit-v9")
    assert custom.scans == Path("/tmp/viewer/scans/trace-tools-unit-v9")
