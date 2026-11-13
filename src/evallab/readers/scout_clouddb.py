"""Blinded DEV DB/ATIF conversations through Inspect Scout 0.5.4, entirely local.

Run with ``uv run --with inspect-scout==0.5.4 python -m
 evallab.readers.scout_clouddb run``. This synchronizes both admitted cloud-DB
DEV traces and the independently assigned CheatBench DEV full trajectories,
scans them, and appends normalized results. No model, upload, or Docker is used.

Only four existing text scanners are supported. The six ``rule_*`` scanners
in research/explorations/trace-lab/scout/scanners.py require
``metadata['trial_dir']`` and are deliberately excluded: DB transcripts have
neither a Harbor trial directory nor identity-bearing transcript metadata.
``restored_tool_calls`` measures representation visibility, not misconduct;
its flags are always null. Publisher labels never enter scanner inputs.

The private key map is append-only and kept outside the Scout transcript DBs.
``sync`` inserts only ids absent from that map; content/reasoning are unbounded.
Each trace is projected on its own: one whose source retains no actor chronology
(``ActorHistoryUnavailable``) is quarantined and reported by run id and reason.
It is never inserted as an empty transcript, never keyed, and never counted as a
clean or scanned run; the other traces in its batch are unaffected. The sync
report keeps full admission (``eligible``) separate from ``inserted``,
``unchanged`` and ``unavailable``.

Every transcript, key-map row, result row and default cache/scan/state path is
versioned by ``ACTOR_PROJECTION_VERSION``. An unversioned transcript cache, key
map or result ledger is refused with ``StaleProjectionError`` and never extended
in place; a key map or transcript cache of another projection is refused too.
Earlier-version result rows stay as retained lineage: they are never counted as
current coverage and never rewritten.

``scan`` accepts only the four deterministic scanners; ``collect`` retains one
record per scanner label (nine records per run with the default scanner set).
Scheduled callers can pass a nonempty ``transcript_ids`` subset to ``scan``;
unselected transcript content is not loaded again.

An unchanged sweep must stay cheap. ``_insert`` opens the Parquet transcript
database only once a transcript actually exists, so a sweep whose unkeyed
remainder is entirely unavailable never imports or opens Scout at all. A caller
that already computed this DB snapshot's admission refs passes them to ``sync``,
so one sweep makes one admission decision. The scheduled watcher's
``ledger_coverage`` reuses a coverage index bound to the SHA-256 of the exact
result-ledger bytes: any content change (including a same-length rewrite)
recomputes coverage, and no index is trusted for a ledger holding unversioned
Scout rows. Admission itself is never cached: its authoritative inputs are the
whole task-identity and split-registry set, and a content digest of those inputs
costs as much as re-deciding admission.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

from evallab.readers import ACTOR_PROJECTION_VERSION

SCOUT_VERSION = "0.5.4"
BLIND_SCHEMA = "evallab.blind_run/v1"
SCANNERS = (
    "repeated_assistant_message",
    "harness_parse_errors",
    "native_tool_call_markup",
    "restored_tool_calls",
)
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_ROOT = Path.home() / ".local/state/evallab-prod-live"
DEFAULT_DB = STATE_ROOT / "cloud-trace-db.sqlite"
#: Shared trace-tool state. Unversioned scout files written here before the
#: native actor projection are retained as read-only evidence; nothing reads or
#: appends to them any more.
TRACE_TOOLS_STATE = STATE_ROOT / "trace-tools"
_VIEWER_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/derived/trace-lab/scout")
PROJECTION_MARKER = "projection.json"
_MESSAGE_STEP = re.compile(r"^step-(-?\d+)-message-\d+$")




class ScoutPaths(NamedTuple):
    """Every location one actor projection reads or writes."""

    state: Path
    keymap: Path
    results: Path
    transcripts: Path
    cheatbench_transcripts: Path
    scans: Path


def scout_paths(
    trace_tools: Path, viewer: Path, projection_version: str = ACTOR_PROJECTION_VERSION,
) -> ScoutPaths:
    """Version-suffixed siblings of the legacy paths.

    Scout discovers Parquet files recursively, so a versioned transcript cache
    must never be nested inside (or contain) another projection's cache.
    """
    state = trace_tools / f"scout-{projection_version}"
    return ScoutPaths(
        state=state,
        keymap=state / "keymap-v1.jsonl",
        results=state / "results-v1.jsonl",
        transcripts=viewer / "data" / f"transcripts_clouddb_dev-{projection_version}",
        cheatbench_transcripts=(
            viewer / "data" / f"transcripts_cheatbench_dev_blind-{projection_version}"
        ),
        scans=viewer / "scans" / f"trace-tools-{projection_version}",
    )


DEFAULT_PATHS = scout_paths(TRACE_TOOLS_STATE, _VIEWER_ROOT)
DEFAULT_STATE = DEFAULT_PATHS.state
DEFAULT_TRANSCRIPTS = DEFAULT_PATHS.transcripts
DEFAULT_CHEATBENCH_TRANSCRIPTS = DEFAULT_PATHS.cheatbench_transcripts
DEFAULT_SCANS = DEFAULT_PATHS.scans
DEFAULT_KEYMAP = DEFAULT_PATHS.keymap
DEFAULT_RESULTS = DEFAULT_PATHS.results






def _scanner_module() -> Any:
    path = DEFAULT_REPO_ROOT / "research/explorations/trace-lab/scout/scanners.py"
    name = "_evallab_trace_tools_scanners"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load deterministic scanners from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    old_path = sys.path[:]
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path
    return module
