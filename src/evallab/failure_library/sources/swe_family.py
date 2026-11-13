"""Pinned, metadata-only imports of SWE task quality evidence.

Builders never execute dataset programs. Published human/judge labels stay labels;
only a pinned differential result is admitted as executed evidence.
"""

from __future__ import annotations

import csv
import hashlib
import urllib.request
from datetime import date
from pathlib import Path

from evallab.failure_library.records import (
    Evidence,
    Finding,
    Split,
    TaskRef,
    derive_tier,
    make_finding_id,
)
from evallab.failure_library.store import default_state_root

OBSERVED = date(2026, 10, 27)
SWE_ORIGINAL_REVISION = "f5351ee8c6663736817027db3ad03fe662cb5bb8"
SWE_VERIFIED_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
OPENAI_URL = (
    "https://cdn.openai.com/introducing-swe-bench-verified/swe-bench-annotation-results.zip"
)
OPENAI_SHA = "b9297c31f6c2ef712218b5bb2830bd47a7f600dedf6b927191d0170177266ab9"


def pinned_bytes(
    collection: str,
    revision: str,
    name: str,
    sha256: str,
    *,
    state_root: Path | None = None,
    url: str | None = None,
    local: Path | None = None,
) -> bytes:
    """Fetch once, verify the pin, then retain immutable source bytes outside Git."""
    state = state_root if state_root is not None else default_state_root()
    path = state / "raw" / collection / revision / name
    if path.is_file():
        data = path.read_bytes()
    elif local is not None and local.is_file():
        data = local.read_bytes()
    elif url is not None:
        request = urllib.request.Request(url, headers={"User-Agent": "evallab-failure-library/1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
    else:
        raise FileNotFoundError(f"Pinned source missing: {path}; seed it from {local}")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"Source SHA-256 mismatch: {collection}/{revision}/{name}")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
        path.chmod(0o444)
    return data


class SplitPolicy:
    """Existing reported splits win; new task families use a shared 80/20 hash."""

    def __init__(self, root: Path) -> None:
        self.assignments: dict[str, set[str]] = {}
        path = root / "research/answer-keys/splits/assignments.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                name = row["task"].split("@", 1)[0].rsplit("/", 1)[-1]
                split = row["split"].lower()
                if "cheatbench" in row["key_id"].lower() or "hackbench" in row["key_id"].lower():
                    split = "cross_test"
                self.assignments.setdefault(name, set()).add(split)

    def get(self, task_id: str, group: str) -> Split:
        bare = task_id.split("@", 1)[0].rsplit("/", 1)[-1]
        existing = self.assignments.get(bare, set())
        precedence: tuple[tuple[str, Split], ...] = (
            ("cross_test", "cross_test"),
            ("heldout", "heldout"),
            ("test", "TEST"),
            ("dev", "DEV"),
        )
        for native, result in precedence:
            if native in existing:
                return result
        return "heldout" if int(hashlib.sha256(group.encode()).hexdigest(), 16) % 5 == 0 else "DEV"


def source_finding(
    collection: str,
    task: TaskRef,
    defect_class: str,
    locator: str,
    summary: str,
    evidence: list[Evidence],
    *,
    finder: str,
    source_url: str,
    split: Split,
    direction: str = "other",
    license: str | None = None,
    fixed_in_revision: str | None = None,
) -> Finding:
    return Finding.model_validate(
        {
            "finding_id": make_finding_id(
                task.source_id, task.task_id, task.upstream_revision or "", defect_class, locator
            ),
            "collection": collection,
            "task": task,
            "defect_class": defect_class,
            "locator": locator,
            "summary": summary[:1000],
            "evidence": evidence,
            "tier": derive_tier(item.kind for item in evidence),
            "finder": finder,
            "source_url": source_url,
            "observed_at": OBSERVED,
            "split": split,
            "direction": direction,
            "license": license,
            "status_now": "fixed_upstream" if fixed_in_revision else "unknown",
            "fixed_in_revision": fixed_in_revision,
        }
    )


def swe_task(task_id: str, *, original: bool = False) -> TaskRef:
    dataset = "SWE-bench" if original else "SWE-bench_Verified"
    return TaskRef(
        source_id="swe-bench",
        task_id=task_id,
        upstream_repo=f"https://huggingface.co/datasets/princeton-nlp/{dataset}",
        upstream_revision=SWE_ORIGINAL_REVISION if original else SWE_VERIFIED_REVISION,
        task_path=f"test/instance_id={task_id}",
    )
