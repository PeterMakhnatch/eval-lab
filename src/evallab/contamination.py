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
