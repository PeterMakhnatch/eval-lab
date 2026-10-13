"""Contamination-audit labels: one verdict per audited agent pass.

The audit (``research/experiments/contamination-audit``) judges every recorded
agent pass and writes ``research/calibration/contamination-audit/labels.jsonl``
(schema ``evallab.contamination_label/v1``), keyed by trial directory name:

- ``verdict``: ``cheat`` | ``clean`` | ``review`` | ``unaudited``;
- ``mechanism``: hack-atlas pattern of a cheat (``upstream_fetch``, ...);
- ``cheat_prompted``: the run's prompt told the agent to cheat;
- ``judges``: per-judge verdicts (``sol``, ``claude``, ``resolution``);
- ``audit_run`` / ``audited_at``: which audit run produced the label.

The labels never change a counts verdict. Readers attach them so reports can
filter: ``evallab trials --sql`` exposes a ``contamination`` relation (join on
``trial``) and the Integrity dashboard counts ``cheat`` labels as cheats.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LABEL_SCHEMA = "evallab.contamination_label/v1"
LABELS_RELPATH = Path("research/calibration/contamination-audit/labels.jsonl")
VERDICTS = ("cheat", "clean", "review", "unaudited")
COLUMNS = (
    "trial",
    "job",
    "family",
    "verdict",
    "mechanism",
    "cheat_prompted",
    "audit_run",
    "audited_at",
)


def labels_path(repo_root: Path | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    return root / LABELS_RELPATH


def load_labels(repo_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Labels by trial directory name; empty when the audit never ran.

    A malformed line or an unknown verdict raises: a silently dropped cheat label
    would read as a clean pass.
    """
    path = labels_path(repo_root)
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("schema") != LABEL_SCHEMA or row.get("verdict") not in VERDICTS:
                raise ValueError(f"{path}:{n}: not a {LABEL_SCHEMA} row with a known verdict")
            out[row["trial"]] = row
    return out
