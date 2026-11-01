"""Worker start-up. Production forks processes; the sandbox only names them and logs evidence."""

from __future__ import annotations

from dispatch.evidence import TamperEvidenceLog


def start_workers(job_id: str, count: int, evidence: TamperEvidenceLog | None) -> list[str]:
    names = [f"{job_id}-w{index}" for index in range(count)]
    if evidence is not None:
        for name in names:
            evidence.append({"event": "worker_start", "worker": name})
    return names
