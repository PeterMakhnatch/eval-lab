"""Join blinded whole-run detector results to DEV labels and cascade decisions.

This module never invokes scanners, uploads traces, or runs models. Label totals
include abstentions; recall/FAR and Wilson intervals use only explicit boolean
results. Overlap counts use paired, non-abstaining tool/cascade decisions, never
interpreting an absent cascade record as a clean decision. ``any_flag`` uses only
flagging detectors (at least one non-null flag in the result store), excluding
always-null informational detectors. Per run it is true if any detector flags,
false if none flags and at least one explicitly returns false, otherwise unknown.

``goodfire_dev_labels`` exposes receipt/corpus DEV labels before the shared
cross-key admission gate. ``scorecard`` applies that gate with its explicit
repository root, and reports counts both before and after admission. CheatBench
DEV is a cross-test set, not a tuning set.

Usage::

    python -m evallab.readers.tool_scorecard --results results-v1.jsonl \
        --out /tmp/scorecard.json --markdown /tmp/scorecard.md
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from evallab.readers import ACTOR_PROJECTION_VERSION

type Flag = bool | None
type Detector = tuple[str, str]
type Cascade = Mapping[str, Mapping[str, Flag]]

_STATE = Path.home() / ".local/state/evallab-prod-live"
DEFAULT_DB = _STATE / "cloud-trace-db.sqlite"
DEFAULT_RESULTS = _STATE / f"trace-tools/scout-{ACTOR_PROJECTION_VERSION}/results-v1.jsonl"
DEFAULT_STAGE1 = _STATE / "trace-tools/cascade-stage1-goodfire-dev.jsonl"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CM9_DIR = (
    Path.home() / "Developer/research-context/bridge/2026-10-25-systems-and-swarms"
    / "raw/cm9-window-budget-20261027"
)
_CM9_FILES = ("e2escore-muse-g.json", "e2escore-muse-cb.json")
_RESULT_SCHEMA = "evallab.trace_tool_result/v1"
