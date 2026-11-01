"""Unified worker configuration schema (used by the metrics sidecar and new services).

Fields default to the lightest settings so a minimal job file is valid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerConfig:
    job_id: str
    model_class: str = "small"
    tier: str = "low"
    workers: int = 1
    evidence_log: str | None = None
    require_tamper_evidence: bool = False


def load_worker_config(path: str | Path) -> WorkerConfig:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return WorkerConfig(
        job_id=data.get("job_id", path.stem),
        model_class=data.get("model_class", "small"),
        tier=data.get("tier", "low"),
        workers=int(data.get("workers", 1)),
        evidence_log=data.get("evidence_log"),
        require_tamper_evidence=bool(data.get("require_tamper_evidence", False)),
    )
