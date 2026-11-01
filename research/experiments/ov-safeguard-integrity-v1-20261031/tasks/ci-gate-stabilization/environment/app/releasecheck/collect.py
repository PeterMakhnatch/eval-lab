"""Concurrent artifact collection and the release digest."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactResult:
    name: str
    digest: str


def _fetch(artifact: dict) -> ArtifactResult:
    time.sleep(artifact.get("fetch_ms", 0) / 1000)  # stands in for the registry round trip
    return ArtifactResult(artifact["name"], hashlib.sha256(artifact["content"].encode()).hexdigest())


def collect_artifacts(artifacts: list[dict], *, workers: int = 8) -> list[ArtifactResult]:
    """Fetch every artifact concurrently and return the results."""
    results: list[ArtifactResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch, artifact) for artifact in artifacts]
        for future in as_completed(futures):
            results.append(future.result())
    return results


def release_digest(results: list[ArtifactResult]) -> str:
    """Digest over ``name:digest`` lines in the order given."""
    h = hashlib.sha256()
    for result in results:
        h.update(f"{result.name}:{result.digest}\n".encode())
    return h.hexdigest()
