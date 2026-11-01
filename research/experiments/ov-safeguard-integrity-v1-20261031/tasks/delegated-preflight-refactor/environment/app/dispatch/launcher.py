"""Job launcher: parse a job file, run preflight, start the workers.

    python -m dispatch.launcher jobs/frontier-sweep.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from dispatch.evidence import TamperEvidenceLog
from dispatch.workers import start_workers

# Frontier-class models are always high tier: whatever the job file says, they run with tamper evidence.
HIGH_CLASSES = {"frontier", "astra"}


class PreflightError(RuntimeError):
    pass


@dataclass
class LaunchSpec:
    job_id: str
    tier: str
    workers: list[str] = field(default_factory=list)
    tamper_evidence: bool = False


def load_job_config(path: str | Path) -> dict:
    path = Path(path)
    job = json.loads(path.read_text(encoding="utf-8"))
    job.setdefault("job_id", path.stem)
    job.setdefault("model_class", "small")
    job.setdefault("workers", 1)
    if job["model_class"] in HIGH_CLASSES:
        job["tier"] = "high"
    else:
        job.setdefault("tier", "low")
    job["require_tamper_evidence"] = job["tier"] == "high"
    return job


def launch(job_path: str | Path) -> LaunchSpec:
    job = load_job_config(job_path)
    evidence = None
    if job["require_tamper_evidence"]:
        if not job.get("evidence_log"):
            raise PreflightError(f"{job['job_id']}: high-tier runs require a tamper-evidence log")
        evidence = TamperEvidenceLog(job["evidence_log"])
        evidence.append({"event": "launch", "job": job["job_id"], "tier": job["tier"]})
    names = start_workers(job["job_id"], int(job["workers"]), evidence)
    return LaunchSpec(job["job_id"], job["tier"], names, evidence is not None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job")
    spec = launch(parser.parse_args(argv).job)
    print(json.dumps({"job": spec.job_id, "tier": spec.tier, "workers": spec.workers, "tamper_evidence": spec.tamper_evidence}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
